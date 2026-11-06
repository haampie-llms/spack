"""Property tests for Spec.splice on random concrete DAGs, without a package repository.

Each case builds a concrete spec, picks a node of it as the target, builds a replacement for that
target, splices, and checks the invariants below. The target is known to the test, not to
Spec.splice, which guesses it from the replacement.

    spack python share/spack/qa/splice_props.py [--cases N] [--seed S] [--transitive yes|no|both]
    spack python share/spack/qa/splice_props.py --seed S --transitive yes --shrink "<invariant>"
"""

import argparse
import collections
import random
import sys
import traceback
from typing import Dict, List, Optional, Set, Tuple

import spack.deptypes as dt
import spack.repo
import spack.spec
from spack.spec import Spec

#: Packages and the virtuals they can provide. glibc can provide more than it is used as.
PACKAGES = {
    "a": (),
    "b": (),
    "c": (),
    "d": (),
    "mpich": ("mpi",),
    "zmpi": ("mpi",),
    "glibc": ("libc", "iconv"),
    "musl": ("libc",),
    "libiconv": ("iconv",),
}
VERSIONS = ("1", "2")
VIRTUALS = sorted({v for vs in PACKAGES.values() for v in vs})
PROVIDERS = {v: [p for p, vs in PACKAGES.items() if v in vs] for v in VIRTUALS}
NON_BUILD = dt.ALL & ~dt.BUILD
LINK_RUN = (dt.LINK, dt.RUN, dt.LINK | dt.RUN, dt.BUILD | dt.LINK)


# ---------------------------------------------------------------------------------------------
# Generating concrete DAGs
# ---------------------------------------------------------------------------------------------


class Pool:
    """Concrete nodes, shared between the spec and the replacement when they are identical."""

    def __init__(self):
        self.nodes: Dict[tuple, Spec] = {}

    def node(self, name: str, version: str, edges) -> Spec:
        key = (name, version, tuple(sorted((id(c), f, v) for c, f, v in edges)))
        if key not in self.nodes:
            s = Spec(f"{name}@={version} arch=linux-ubuntu26.04-x86_64")
            s._provided_virtuals = tuple(Spec(v) for v in PACKAGES[name])
            s._package_hash = f"{name}{version}".ljust(32, "0")
            for child, depflag, virtuals in edges:
                s._add_dependency(child, depflag=depflag, virtuals=virtuals)
            s._mark_concrete()
            self.nodes[key] = s
        return self.nodes[key]


def random_dag(
    rng: random.Random,
    pool: Pool,
    root_name: str,
    root_version: str,
    avoid_names: Set[str] = set(),
    avoid_virtuals: Set[str] = set(),
) -> Spec:
    """A DAG a solve could produce: in its link/run part one node per name and one provider per
    virtual. Some nodes also get a build dependency, which may reuse a name."""
    # The provider of each virtual, if the DAG uses it
    used_as: Dict[str, Set[str]] = collections.defaultdict(set)
    for v in VIRTUALS:
        provider = rng.choice(PROVIDERS[v])
        if v in avoid_virtuals or provider == root_name or provider in avoid_names:
            continue
        if rng.random() < 0.6:
            used_as[provider].add(v)
    plain = [p for p in PACKAGES if p != root_name and p not in used_as and p not in avoid_names]
    names = list(used_as) + rng.sample(
        plain, k=rng.randint(min(1, len(plain)), min(3, len(plain)))
    )
    rng.shuffle(names)
    order = [(root_name, root_version)] + [(n, rng.choice(VERSIONS)) for n in names]

    # Edges from earlier to later nodes, each node reachable from the root
    edges: Dict[int, List[Tuple[int, int, tuple]]] = collections.defaultdict(list)
    for child in range(1, len(order)):
        parents = {rng.randrange(child)} | {p for p in range(child) if rng.random() < 0.25}
        for parent in parents:
            virtuals = sorted(used_as[order[child][0]])
            if virtuals and rng.random() < 0.8:
                virtuals = rng.sample(virtuals, k=rng.randint(1, len(virtuals)))
            else:
                virtuals = []
            edges[parent].append((child, rng.choice(LINK_RUN), tuple(virtuals)))

    # Build bottom-up, adding the occasional build-only leaf
    built: Dict[int, Spec] = {}
    for i in reversed(range(len(order))):
        name, version = order[i]
        node_edges = [(built[c], f, v) for c, f, v in edges[i]]
        if rng.random() < 0.3:
            build_dep = pool.node(rng.choice(list(PACKAGES)), rng.choice(VERSIONS), [])
            if all(build_dep is not c for c, _, _ in node_edges):
                node_edges.append((build_dep, dt.BUILD, ()))
        built[i] = pool.node(name, version, node_edges)
    return built[0]


def used_as(spec: Spec, node: Spec) -> Set[str]:
    """Virtuals that node is used as on non-build edges within spec"""
    in_dag = {id(n) for n in spec.traverse(deptype=NON_BUILD, key=id)}
    return {
        v
        for e in node.edges_from_dependents()
        if id(e.parent) in in_dag and e.depflag & NON_BUILD
        for v in e.virtuals
    }


def random_case(rng: random.Random):
    pool = Pool()
    root = rng.choice(list(PACKAGES))
    spec = random_dag(rng, pool, root, rng.choice(VERSIONS))
    candidates = list(spec.traverse(root=False, deptype=NON_BUILD, key=id))
    if not candidates:
        return None
    target = rng.choice(candidates)

    # Replace the target by another version of it, or by another provider of what it is used as
    virtuals = used_as(spec, target)
    swaps = [p for p in PACKAGES if p != target.name and virtuals and virtuals <= set(PACKAGES[p])]
    if swaps and rng.random() < 0.4:
        name, version = rng.choice(swaps), rng.choice(VERSIONS)
    else:
        name, version = target.name, next(v for v in VERSIONS if v != str(target.version))
    # Mostly avoid what the ancestors of the target use, which makes a splice impossible
    avoid_names, avoid_virtuals = set(), set()
    if rng.random() < 0.9:
        for n in spec.traverse(deptype=NON_BUILD, key=id):
            if any(x is target for x in n.traverse(root=False, deptype=NON_BUILD, key=id)):
                avoid_names.add(n.name)
                avoid_virtuals |= used_as(spec, n)
    replacement = random_dag(rng, pool, name, version, avoid_names, avoid_virtuals)
    return spec, target, replacement


# ---------------------------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------------------------


def fresh_copy(spec: Spec) -> Spec:
    """Deep copy, build specs included, without any cached hash"""
    c = spec.copy()
    for n in c.traverse(key=id):
        if n._build_spec is not None:
            n._build_spec = fresh_copy(n._build_spec)
        n.clear_caches(keep_package_hash=True)
    return c


def recomputed_hashes(spec: Spec) -> Dict[int, str]:
    """Hash recomputed from the graph for every node of spec and of its build specs, by id"""
    result: Dict[int, str] = {}

    def visit(orig: Spec, copy: Spec):
        for a, b in zip(orig.traverse(key=id), copy.traverse(key=id)):
            result[id(a)] = b.dag_hash()
            if a._build_spec is not None:
                visit(a._build_spec, b._build_spec)

    visit(spec, fresh_copy(spec))
    return result


def all_nodes(spec: Spec) -> List[Spec]:
    """Nodes of spec and of its build specs"""
    result, seen = [], set()

    def visit(root):
        for n in root.traverse(key=id):
            if id(n) not in seen:
                seen.add(id(n))
                result.append(n)
                if n._build_spec is not None:
                    visit(n._build_spec)

    visit(spec)
    return result


def fingerprint(spec: Spec):
    """What must not change: hashes, serialization, and the dependents of every node"""
    nodes = all_nodes(spec)
    dependents = [sorted(id(e.parent) for e in n.edges_from_dependents()) for n in nodes]
    return [(n.name, n.dag_hash()) for n in nodes], dependents, spec.to_json()


# ---------------------------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------------------------


class Case:
    """The inputs and output of one splice, with recomputed hashes of everything involved"""

    def __init__(self, spec, target, replacement, transitive, out):
        self.spec, self.target, self.replacement = spec, target, replacement
        self.transitive, self.out = transitive, out
        self.rh = {**recomputed_hashes(spec), **recomputed_hashes(replacement)}
        self.rh.update(recomputed_hashes(out))
        self.spec_hashes = {self.rh[id(n)] for n in all_nodes(spec)}
        self.replacement_hashes = {self.rh[id(n)] for n in all_nodes(replacement)}

    def h(self, node: Spec) -> str:
        return self.rh[id(node)]

    def origin(self, node: Spec) -> Spec:
        """The input node a node of the output stands for: its build spec if spliced"""
        return node._build_spec if node._build_spec is not None else node


def inv1_one_edge_per_pair(c: Case):
    """1. A parent has at most one edge to a given child; dependency types are merged."""
    for n in all_nodes(c.out):
        counts = collections.Counter(c.h(e.spec) for e in n.edges_to_dependencies())
        for e in n.edges_to_dependencies():
            if counts[c.h(e.spec)] > 1:
                yield f"{n.name} has {counts[c.h(e.spec)]} edges to {e.spec.name}"


def inv2_unique_names_and_providers(c: Case):
    """2. In the link/run closure of any node: one node per name, one provider per virtual."""
    for n in c.out.traverse(key=id):
        closure = list(n.traverse(deptype=NON_BUILD, key=id))
        by_name = collections.defaultdict(set)  # copies of one node have one hash
        for x in closure:
            by_name[x.name].add(c.h(x))
        for name, hashes in by_name.items():
            if len(hashes) > 1:
                yield f"closure of {n.name} has {len(hashes)} nodes named {name}"
        providers = collections.defaultdict(set)
        for x in closure:
            for e in x.edges_to_dependencies(depflag=NON_BUILD):
                for v in e.virtuals:
                    providers[v].add(e.spec.name)
        for v, names in providers.items():
            if len(names) > 1:
                yield f"closure of {n.name} has {sorted(names)} as {v}"


def inv3_edge_virtuals_are_provided(c: Case):
    """3. A child provides the virtuals on its in-edges."""
    for n in all_nodes(c.out):
        for e in n.edges_to_dependencies():
            missing = set(e.virtuals) - {v.name for v in e.spec.provided_virtuals}
            if missing:
                yield f"{n.name} -> {e.spec.name} as {sorted(missing)}, which it cannot provide"


def inv4_build_spec_is_an_input(c: Case):
    """4. The build spec of a spliced node is a node of the inputs, not a new one."""
    for n in all_nodes(c.out):
        if n.spliced and c.h(n.build_spec) not in c.spec_hashes | c.replacement_hashes:
            yield f"build spec of {n.name} is not an input node"


def inv5_spliced_differs_only_in_dependencies(c: Case):
    """5. A spliced node and its build spec agree on everything but dependencies."""
    strip = ("dependencies", "hash", "build_spec")
    for n in all_nodes(c.out):
        if n.spliced:
            a = {k: v for k, v in n.to_node_dict().items() if k not in strip}
            b = {k: v for k, v in n.build_spec.to_node_dict().items() if k not in strip}
            if a != b:
                keys = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
                yield f"{n.name} differs from its build spec in {keys}"


def inv6_spliced_nodes_have_no_build_deps(c: Case):
    """6. Build dependencies of a spliced node live on its build spec only."""
    for n in all_nodes(c.out):
        if n.spliced and n.edges_to_dependencies(depflag=dt.BUILD):
            yield f"spliced {n.name} has build edges"


def inv7_spliced_iff_dependencies_changed(c: Case):
    """7. A node is spliced iff its link/run dependencies differ from those of its build spec;
    a node that is not spliced is an input node."""
    for n in all_nodes(c.out):
        if n.spliced:
            mine = sorted(c.h(e.spec) for e in n.edges_to_dependencies(depflag=NON_BUILD))
            built = sorted(
                c.h(e.spec) for e in n.build_spec.edges_to_dependencies(depflag=NON_BUILD)
            )
            if mine == built:
                yield f"{n.name} is spliced but has the dependencies of its build spec"
        elif c.h(n) not in c.spec_hashes | c.replacement_hashes:
            yield f"{n.name} is not spliced but is not an input node either"


def inv8_cached_hashes_are_correct(c: Case):
    """8. Cached hashes equal hashes recomputed from the graph, build specs included."""
    for n in all_nodes(c.out):
        if n.dag_hash() != c.h(n):
            yield f"{n.name}: cached {n.dag_hash()[:7]}, recomputed {c.h(n)[:7]}"


def edge_image(p: Spec, e) -> Optional[object]:
    """The edge of spliced p that edge e of its build spec maps to: same child name, or else
    the same virtuals"""
    for f in p.edges_to_dependencies():
        if f.spec.name == e.spec.name:
            return f
    for f in p.edges_to_dependencies():
        if e.virtuals and f.virtuals == e.virtuals:
            return f
    return None


def inv10_edges_are_preserved(c: Case):
    """10. Every link/run edge of the build spec of a spliced node maps to an edge of the node
    with the same dependency types (without build) and virtuals."""
    for p in all_nodes(c.out):
        if not p.spliced:
            continue
        built = p.build_spec.edges_to_dependencies(depflag=NON_BUILD)
        mine = p.edges_to_dependencies()
        if len(built) != len(mine):
            yield f"{p.name} has {len(mine)} edges, its build spec {len(built)}"
        for e in built:
            f = edge_image(p, e)
            if f is None:
                yield f"edge {p.name} -> {e.spec.name} of the build spec is gone"
            elif f.depflag != e.depflag & NON_BUILD or f.virtuals != e.virtuals:
                yield (
                    f"edge {p.name} -> {e.spec.name} {dt.flag_to_tuple(e.depflag)} {e.virtuals} "
                    f"became {dt.flag_to_tuple(f.depflag)} {f.virtuals}"
                )


def inv11_only_target_and_clashes_are_replaced(c: Case):
    """11. A dependency is replaced only if it is the target, or if it clashes by name or by
    virtual with a node of the other side; clashes go to the replacement when transitive, and to
    the original spec otherwise."""
    winner = c.replacement_hashes if c.transitive else c.spec_hashes
    for p in all_nodes(c.out):
        if not p.spliced:
            continue
        for e in p.build_spec.edges_to_dependencies(depflag=NON_BUILD):
            f = edge_image(p, e)
            if f is None:
                continue
            old, new = e.spec, f.spec
            if c.h(new) == c.h(old) or c.h(c.origin(new)) == c.h(old):
                continue  # same node, or the same node rewired
            if c.h(old) == c.h(c.target) and c.h(c.origin(new)) == c.h(c.replacement):
                continue  # the target, replaced as requested
            clash = new.name == old.name or bool(e.virtuals)
            if clash and c.h(c.origin(new)) in winner:
                continue
            side = "replacement" if c.h(c.origin(new)) in c.replacement_hashes else "spec"
            old_str, new_str = f"{old.name}@{old.version}", f"{new.name}@{new.version}"
            yield f"{p.name} -> {old_str} replaced by {new_str} from the {side}"


def inv12_target_replaced(c: Case):
    """12. The target is gone from the link/run DAG, and the replacement is in it (rewired if
    not transitive)."""
    closure = list(c.out.traverse(deptype=NON_BUILD, key=id))
    if any(c.h(n) == c.h(c.target) for n in closure):
        yield f"target {c.target.name}@{c.target.version} is still there"
    if not any(c.h(c.origin(n)) == c.h(c.replacement) for n in closure):
        yield f"replacement {c.replacement.name}@{c.replacement.version} is missing"


INVARIANTS = [
    inv1_one_edge_per_pair,
    inv2_unique_names_and_providers,
    inv3_edge_virtuals_are_provided,
    inv4_build_spec_is_an_input,
    inv5_spliced_differs_only_in_dependencies,
    inv6_spliced_nodes_have_no_build_deps,
    inv7_spliced_iff_dependencies_changed,
    inv8_cached_hashes_are_correct,
    inv10_edges_are_preserved,
    inv11_only_target_and_clashes_are_replaced,
    inv12_target_replaced,
]


def has_cycle(spec: Spec) -> bool:
    state: Dict[int, int] = {}

    def visit(n):
        state[id(n)] = 1
        for e in n.edges_to_dependencies():
            s = state.get(id(e.spec))
            if s == 1 or (s is None and visit(e.spec)):
                return True
        state[id(n)] = 2
        return False

    return visit(spec)


def impossible(spec: Spec, target: Spec, replacement: Spec) -> Optional[str]:
    """Why no valid splice exists: the replacement uses the name of an ancestor of the target,
    or a virtual it is used as, so the ancestor would have to depend on itself."""
    ancestors = [
        n
        for n in spec.traverse(deptype=NON_BUILD, key=id)
        if any(x is target for x in n.traverse(root=False, deptype=NON_BUILD, key=id))
    ]
    replacement_dag = list(replacement.traverse(deptype=NON_BUILD, key=id))
    names = {n.name for n in ancestors} & {n.name for n in replacement_dag}
    in_replacement = set().union(*(used_as(replacement, n) for n in replacement_dag))
    virtuals = set().union(*(used_as(spec, a) for a in ancestors)) & in_replacement
    if names or virtuals:
        return f"the replacement uses {sorted(names | virtuals)} of an ancestor of the target"
    # The replacement takes over what the target is used as, so its DAG cannot provide it again
    taken = used_as(spec, target) & in_replacement
    if taken:
        return f"the replacement's DAG provides {sorted(taken)}, which the target is used as"
    return None


def check(spec, target, replacement, transitive) -> Dict[str, List[str]]:
    """Splice and check every invariant, plus 9 (inputs unchanged) and 13 (determinism)"""
    before = fingerprint(spec), fingerprint(replacement)
    reason = impossible(spec, target, replacement)
    try:
        out = spec.splice(replacement, transitive=transitive)
    except spack.spec.SpliceError as e:
        return {} if reason else {"splice raised SpliceError": [str(e)]}
    except Exception as e:
        return {"splice crashed": [f"{type(e).__name__}: {e}"[:200]]}
    if reason:
        cyclic = " and returned a cyclic graph" if has_cycle(out) else ""
        return {
            "0. an impossible splice is rejected": [f"{reason}, but splice accepted it{cyclic}"]
        }
    if has_cycle(out):
        return {"0. the result is acyclic": ["splice returned a cyclic graph"]}
    violations: Dict[str, List[str]] = {}
    if (fingerprint(spec), fingerprint(replacement)) != before:
        violations["9. the inputs are unchanged"] = ["spec or replacement changed"]
    case = Case(spec, target, replacement, transitive, out)
    for inv in INVARIANTS:
        found = list(inv(case))
        if found:
            violations[inv.__doc__.splitlines()[0]] = found
    again = spec.splice(replacement, transitive=transitive)
    if recomputed_hashes(again)[id(again)] != case.h(out):
        violations["13. splicing is deterministic"] = ["second splice differs"]
    return violations


# ---------------------------------------------------------------------------------------------
# Shrinking
# ---------------------------------------------------------------------------------------------

#: A DAG as data: nodes (name, version) with the root first, and edges (parent, child, depflag,
#: virtuals) between node indices
Graph = Tuple[Tuple[Tuple[str, str], ...], Tuple[Tuple[int, int, int, tuple], ...]]


def to_graph(spec: Spec) -> Tuple[Graph, List[Spec]]:
    order = list(spec.traverse(key=id))
    index = {id(n): i for i, n in enumerate(order)}
    nodes = tuple((n.name, str(n.version)) for n in order)
    edges = tuple(
        (index[id(n)], index[id(e.spec)], e.depflag, tuple(e.virtuals))
        for n in order
        for e in n.edges_to_dependencies()
    )
    return (nodes, edges), order


def build(pool: Pool, graph: Graph) -> List[Optional[Spec]]:
    """Concrete specs of the nodes reachable from the root, by index"""
    nodes, edges = graph
    children = collections.defaultdict(list)
    for p, c, f, v in edges:
        children[p].append((c, f, v))
    built: Dict[int, Spec] = {}

    def make(i):
        if i not in built:
            name, version = nodes[i]
            built[i] = pool.node(name, version, [(make(c), f, v) for c, f, v in children[i]])
        return built[i]

    make(0)
    return [built.get(i) for i in range(len(nodes))]


def smaller(graph: Graph):
    """Graphs with one node, edge, virtual or dependency type less"""
    nodes, edges = graph
    for i in range(1, len(nodes)):
        yield nodes, tuple(e for e in edges if i not in e[:2])
    for k in range(len(edges)):
        yield nodes, edges[:k] + edges[k + 1 :]
    for k, (p, c, f, v) in enumerate(edges):
        simpler = [f & NON_BUILD] if f & dt.BUILD and f & NON_BUILD else []
        simpler += [b for b in (dt.LINK, dt.RUN) if f == dt.LINK | dt.RUN]
        simpler = [(p, c, g, v) for g in simpler]
        simpler += [(p, c, f, tuple(x for x in v if x != y)) for y in v]
        for e in simpler:
            yield nodes, edges[:k] + (e,) + edges[k + 1 :]


def shrink(spec: Spec, target: Spec, replacement: Spec, transitive: bool, invariant: str):
    """Greedily remove parts of the case while the given invariant still fails"""

    def materialize(g_spec, g_replacement, target_index):
        pool = Pool()
        spec_nodes, replacement_nodes = build(pool, g_spec), build(pool, g_replacement)
        new_target = spec_nodes[target_index]
        if new_target is None or new_target is spec_nodes[0]:
            return None
        if not any(n is new_target for n in spec_nodes[0].traverse(deptype=NON_BUILD, key=id)):
            return None
        return spec_nodes[0], new_target, replacement_nodes[0]

    def fails(case) -> bool:
        return case is not None and any(k.startswith(invariant) for k in check(*case, transitive))

    def normalize(case):
        (g_spec, order), (g_replacement, _) = to_graph(case[0]), to_graph(case[2])
        return g_spec, g_replacement, next(i for i, n in enumerate(order) if n is case[1])

    state = normalize((spec, target, replacement))
    progress = True
    while progress:
        progress = False
        g_spec, g_replacement, t = state
        candidates = [(g, g_replacement, t) for g in smaller(g_spec)]
        candidates += [(g_spec, g, t) for g in smaller(g_replacement)]
        for candidate in candidates:
            # Splicing may modify its inputs, so check and normalize separate copies
            if fails(materialize(*candidate)):
                state, progress = normalize(materialize(*candidate)), True
                break
    return materialize(*state)


# ---------------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------------


def describe(spec: Spec) -> str:
    """One line per node and per edge; nodes are labeled by hash, or by object if cyclic"""
    cyclic = has_cycle(spec)
    label = lambda n: (
        f"{n.name}@{n.version}/" + (f"#{id(n) % 1000:03d}" if cyclic else n.dag_hash()[:5])
    )
    lines, seen, stack = [], set(), [spec]
    while stack:
        n = stack.pop(0)
        if id(n) in seen:
            continue
        seen.add(id(n))
        tag = f" (spliced from {n.build_spec.name}@{n.build_spec.version})" if n.spliced else ""
        lines.append(f"    {label(n)}{tag}")
        for e in n.edges_to_dependencies():
            v = f" as {','.join(e.virtuals)}" if e.virtuals else ""
            lines.append(f"      -> {label(e.spec)} [{','.join(dt.flag_to_tuple(e.depflag))}]{v}")
            stack.append(e.spec)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--transitive", choices=("yes", "no", "both"), default="both")
    parser.add_argument("--show", type=int, default=1, help="examples to print per invariant")
    parser.add_argument("--shrink", metavar="INVARIANT", help="minimize --seed for this invariant")
    args = parser.parse_args()
    modes = {"yes": [True], "no": [False], "both": [True, False]}[args.transitive]

    if args.shrink:
        with spack.repo.use_repositories():
            spec, target, replacement = random_case(random.Random(args.seed))
            spec, target, replacement = shrink(spec, target, replacement, modes[0], args.shrink)
            print(f"seed {args.seed}, transitive={modes[0]}")
            print(f"  target {target.name}@{target.version}/{target.dag_hash()[:5]}")
            print(f"  spec\n{describe(spec)}\n  replacement\n{describe(replacement)}")
            try:
                print(f"  result\n{describe(spec.splice(replacement, transitive=modes[0]))}")
            except Exception as e:
                print(f"  result: {type(e).__name__}")
            for inv, msgs in check(spec, target, replacement, modes[0]).items():
                print(f"  ! {inv.split(chr(10))[0][:60]}: {msgs[0]}")
        return

    failures = collections.defaultdict(list)  # invariant -> [(size, seed, transitive, msgs)]
    checked = 0
    with spack.repo.use_repositories():
        for seed in range(args.seed, args.seed + args.cases):
            case = random_case(random.Random(seed))
            if case is None:
                continue
            spec, target, replacement = case
            for transitive in modes:
                checked += 1
                for inv, msgs in check(spec, target, replacement, transitive).items():
                    size = len(all_nodes(spec)) + len(all_nodes(replacement))
                    failures[inv].append((size, seed, transitive, msgs))

        print(f"{checked} splices checked\n")
        for inv, cases in sorted(failures.items()):
            print(f"{len(cases):5d}  {inv}")
        for inv, cases in sorted(failures.items()):
            for size, seed, transitive, msgs in sorted(cases)[: args.show]:
                spec, target, replacement = random_case(random.Random(seed))
                print(f"\n=== {inv}\nseed {seed}, transitive={transitive}")
                print(f"  target {target.name}@{target.version}/{target.dag_hash()[:5]}")
                print(f"  spec\n{describe(spec)}\n  replacement\n{describe(replacement)}")
                try:
                    print(f"  result\n{describe(spec.splice(replacement, transitive=transitive))}")
                except Exception as e:
                    print(f"  result: {type(e).__name__}")
                for m in msgs[:5]:
                    print(f"  ! {m}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
