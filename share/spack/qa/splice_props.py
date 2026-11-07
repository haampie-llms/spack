"""Property tests for Spec.splice on random concrete DAGs, without a package repository.

Each case builds a concrete spec, picks a node of it as the target, builds a replacement for that
target, splices, and checks the invariants below. Inputs include specs and replacements that are
themselves spliced, rebuilds of the same version, compilers as build dependencies with virtuals,
build dependencies with DAGs of their own, test edges, variants and externals. Besides the
structure of the result, the checks cover the root, a JSON round trip, splicing twice, splicing
the target back, edge order, agreement of the two modes without clashes, commutation of
independent splices, and that relocation can map the dependencies of each spliced node's build
spec onto its own.

The target is passed to Spec.splice when it accepts one; with --guess, or when it does not,
Spec.splice guesses it from the replacement, and the test expects the documented guess. Which
splices are impossible follows the semantics of the rewritten Spec.splice: clashes go to the
replacement when transitive and to the spec otherwise, resolved by name and by virtual. Splices
it rejects because relocation cannot map their result must fail invariant 21 without that check.

    spack python share/spack/qa/splice_props.py [--cases N] [--seed S] [--transitive yes|no|both]
        [--guess] [--jobs J] [--show K] [--only INVARIANT]
    spack python share/spack/qa/splice_props.py --seed S --transitive yes --shrink "<invariant>"
"""

import argparse
import collections
import inspect
import multiprocessing
import os
import random
import sys
import traceback
from typing import Dict, List, Optional, Set, Tuple

import spack.deptypes as dt
import spack.repo
import spack.spec
import spack.traverse
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
    "gcc": ("c", "cxx"),  # only used as a build dependency
}
LINK_PACKAGES = [p for p in PACKAGES if p != "gcc"]
VERSIONS = ("1", "2")
VIRTUALS = sorted({v for p in LINK_PACKAGES for v in PACKAGES[p]})
PROVIDERS = {v: [p for p in LINK_PACKAGES if v in PACKAGES[p]] for v in VIRTUALS}
NON_BUILD = dt.ALL & ~dt.BUILD
LINK_RUN = (dt.LINK, dt.RUN, dt.LINK | dt.RUN, dt.BUILD | dt.LINK, dt.TEST, dt.LINK | dt.TEST)
#: Whether Spec.splice takes the node to replace
HAS_TARGET = "target" in inspect.signature(Spec.splice).parameters
#: Whether the test passes the target to Spec.splice, set by main
EXPLICIT = HAS_TARGET

# ---------------------------------------------------------------------------------------------
# Generating concrete DAGs
# ---------------------------------------------------------------------------------------------


class Pool:
    """Concrete nodes, shared between the spec and the replacement when they are identical."""

    def __init__(self):
        self.nodes: Dict[tuple, Spec] = {}

    def node(self, name, version, edges, variant="", external=False) -> Spec:
        key = (name, version, variant, external, tuple(sorted((id(c), f, v) for c, f, v in edges)))
        if key not in self.nodes:
            s = Spec(f"{name}@={version}{variant} arch=linux-ubuntu26.04-x86_64")
            s._provided_virtuals = tuple(Spec(v) for v in PACKAGES[name])
            s._package_hash = f"{name}{version}".ljust(32, "0")
            if external:
                s.external_path = f"/ext/{name}"
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
    avoid_names: Set[str] = frozenset(),
    avoid_virtuals: Set[str] = frozenset(),
    build_deps: bool = True,
) -> Spec:
    """A DAG a solve could produce: in its link/run part one node per name and one provider per
    virtual. Nodes may get a build dependency: a compiler, a tool with its own DAG, or a leaf."""
    used_as: Dict[str, Set[str]] = collections.defaultdict(set)
    for v in VIRTUALS:
        provider = rng.choice(PROVIDERS[v])
        if v in avoid_virtuals or provider == root_name or provider in avoid_names:
            continue
        if rng.random() < 0.6:
            used_as[provider].add(v)
    plain = [
        p for p in LINK_PACKAGES if p != root_name and p not in used_as and p not in avoid_names
    ]
    names = list(used_as) + rng.sample(
        plain, k=rng.randint(min(1, len(plain)), min(3, len(plain)))
    )
    rng.shuffle(names)
    order = [(root_name, root_version)] + [(n, rng.choice(VERSIONS)) for n in names]
    edges = collections.defaultdict(list)
    for child in range(1, len(order)):
        parents = {rng.randrange(child)} | {p for p in range(child) if rng.random() < 0.25}
        for parent in parents:
            virtuals = sorted(used_as[order[child][0]])
            if virtuals and rng.random() < 0.8:
                virtuals = rng.sample(virtuals, k=rng.randint(1, len(virtuals)))
            else:
                virtuals = []
            edges[parent].append((child, rng.choice(LINK_RUN), tuple(virtuals)))

    built: Dict[int, Spec] = {}
    for i in reversed(range(len(order))):
        name, version = order[i]
        node_edges = [(built[c], f, v) for c, f, v in edges[i]]
        if build_deps and rng.random() < 0.4:
            r = rng.random()
            if r < 0.4:  # a compiler, with its own link deps
                gcc = random_dag(rng, pool, "gcc", rng.choice(VERSIONS), build_deps=False)
                dep, virtuals = gcc, tuple(rng.choice([("c",), ("c", "cxx")]))
            elif r < 0.7:  # a build tool with its own DAG
                tool = rng.choice(LINK_PACKAGES)
                dep = random_dag(rng, pool, tool, rng.choice(VERSIONS), build_deps=False)
                virtuals = ()
            else:
                dep = pool.node(rng.choice(LINK_PACKAGES), rng.choice(VERSIONS), [])
                virtuals = ()
            if all(dep is not c for c, _, _ in node_edges):
                node_edges.append((dep, dt.BUILD, virtuals))
        variant = rng.choice(["", "+shared", "~shared"])
        external = not node_edges and rng.random() < 0.1
        built[i] = pool.node(name, version, node_edges, variant=variant, external=external)
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


def ancestors_of(spec: Spec, targets) -> List[Spec]:
    """Link/run nodes of spec from which a target is reachable"""
    tid = {id(t) for t in targets}
    return [
        n
        for n in spec.traverse(deptype=NON_BUILD, key=id)
        if any(id(x) in tid for x in n.traverse(root=False, deptype=NON_BUILD, key=id))
    ]


def pick_replacement(rng: random.Random, pool: Pool, spec: Spec, target: Spec) -> Spec:
    """Another provider of what the target is used as, a rebuild of the same version with other
    dependencies, or another version. Mostly avoid what the ancestors of the target use, which
    makes a splice impossible."""
    virtuals = used_as(spec, target)
    swaps = [
        p for p in LINK_PACKAGES if p != target.name and virtuals and virtuals <= set(PACKAGES[p])
    ]
    r = rng.random()
    if swaps and r < 0.3:
        name, version = rng.choice(swaps), rng.choice(VERSIONS)
    elif r < 0.5:
        name, version = target.name, str(target.version)
    else:
        name, version = target.name, next(v for v in VERSIONS if v != str(target.version))
    avoid_names, avoid_virtuals = set(), set()
    if target is not spec and rng.random() < 0.9:
        for n in ancestors_of(spec, [target]):
            avoid_names.add(n.name)
            avoid_virtuals |= used_as(spec, n)
    return random_dag(rng, pool, name, version, avoid_names, avoid_virtuals)


def presplice(rng, pool, spec) -> Spec:
    """Turn spec into the result of an earlier splice, if one succeeds"""
    candidates = list(spec.traverse(root=False, deptype=NON_BUILD, key=id))
    if not candidates:
        return spec
    t = rng.choice(candidates)
    r = pick_replacement(rng, pool, spec, t)
    try:
        return splice_at(spec, r, rng.random() < 0.5, t)
    except spack.spec.SpliceError:
        return spec


def guessed_targets(spec: Spec, replacement: Spec) -> List[Spec]:
    """What Spec.splice documents it replaces without an explicit target"""
    nodes = list(spec.traverse(deptype=NON_BUILD, key=spack.traverse.by_dag_hash))
    named = [n for n in nodes if n.name == replacement.name]
    if named:
        return named
    provided = {v.name for v in replacement.provided_virtuals}
    out = []
    for n in nodes:
        u = used_as(spec, n) if n is not spec else set()
        if u and u <= provided:
            out.append(n)
    return out


def random_case(rng: random.Random, explicit: bool):
    """A spec, the nodes to replace, and a replacement; sometimes the root is the target, and
    when the target is guessed, sometimes the replacement is unrelated to the spec"""
    pool = Pool()
    root = rng.choice(LINK_PACKAGES)
    spec = random_dag(rng, pool, root, rng.choice(VERSIONS))
    if rng.random() < 0.25:
        spec = presplice(rng, pool, spec)
    candidates = list(spec.traverse(root=False, deptype=NON_BUILD, key=id))
    if not candidates:
        return None
    roll = rng.random()
    if roll < 0.05:
        target = spec
    else:
        target = rng.choice(candidates)
    if not explicit and roll > 0.95:  # an unrelated replacement
        present = {n.name for n in spec.traverse(deptype=NON_BUILD)}
        free = [p for p in "abcd" if p not in present]
        if not free:
            return None
        replacement = random_dag(rng, pool, rng.choice(free), "1")
    else:
        replacement = pick_replacement(rng, pool, spec, target)
    if rng.random() < 0.15:
        replacement = presplice(rng, pool, replacement)
    targets = [target] if explicit else guessed_targets(spec, replacement)
    return spec, targets, replacement


# ---------------------------------------------------------------------------------------------
# Hashes and helpers
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

    def visit(orig, copy):
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


def link_run(spec: Spec) -> List[Spec]:
    return list(spec.traverse(deptype=NON_BUILD, key=id))


def splice_at(spec: Spec, replacement: Spec, transitive: bool, target: Spec) -> Spec:
    """Splice, passing the target if Spec.splice takes one"""
    if HAS_TARGET:
        return spec.splice(replacement, transitive=transitive, target=target)
    return spec.splice(replacement, transitive=transitive)


def do_splice(spec, targets, replacement, transitive):
    if EXPLICIT:
        return spec.splice(replacement, transitive=transitive, target=targets[0])
    return spec.splice(replacement, transitive=transitive)


def reversed_copy(spec: Spec) -> Spec:
    """Same DAG, edges inserted in reverse order"""
    memo: Dict[int, Spec] = {}

    def rc(n):
        if id(n) not in memo:
            new = n.copy(deps=False)
            for e in reversed(n.edges_to_dependencies()):
                new._add_dependency(rc(e.spec), depflag=e.depflag, virtuals=e.virtuals)
            if n._build_spec is not None:
                new._build_spec = rc(n._build_spec)
            memo[id(n)] = new
        return memo[id(n)]

    return rc(spec)


# ---------------------------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------------------------


class Case:
    """The inputs and output of one splice, with recomputed hashes of everything involved"""

    def __init__(self, spec, targets, replacement, transitive, out):
        self.spec, self.targets, self.replacement = spec, targets, replacement
        self.transitive, self.out = transitive, out
        self.rh = {**recomputed_hashes(spec), **recomputed_hashes(replacement)}
        self.rh.update(recomputed_hashes(out))
        self.inputs = all_nodes(spec) + all_nodes(replacement)
        self.link_run_inputs = link_run(spec) + link_run(replacement)
        self.spec_hashes = {self.rh[id(n)] for n in all_nodes(spec)}
        self.replacement_hashes = {self.rh[id(n)] for n in all_nodes(replacement)}
        self.input_hashes = self.spec_hashes | self.replacement_hashes
        self.target_hashes = {self.rh[id(t)] for t in targets}
        self.replacement_dag_hashes = {self.rh[id(n)] for n in link_run(replacement)}

    def h(self, node):
        return self.rh[id(node)]

    def origins(self, n) -> List[Spec]:
        """Input nodes that output node n stands for"""
        same = [
            x
            for x in self.inputs
            if self.h(x) == self.h(n) or self.h(x.build_spec) == self.h(n)  # collapsed
        ]
        if same or not n.spliced:
            return same
        return [
            x
            for x in self.link_run_inputs
            if x.name == n.name and self.h(x.build_spec) == self.h(n.build_spec)
        ]

    def stands_for(self, n, x) -> bool:
        return any(self.h(o) == self.h(x) for o in self.origins(n))


def inv1_one_edge_per_pair(c):
    """1. A parent has at most one edge to a given child."""
    for n in all_nodes(c.out):
        counts = collections.Counter(c.h(e.spec) for e in n.edges_to_dependencies())
        for e in n.edges_to_dependencies():
            if counts[c.h(e.spec)] > 1:
                yield f"{n.name} has {counts[c.h(e.spec)]} edges to {e.spec.name}"


def inv2_unique_names_and_providers(c):
    """2. In the link/run closure of any node: one node per name, one provider per virtual."""
    for n in c.out.traverse(key=id):
        closure = link_run(n)
        by_name = collections.defaultdict(set)
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


def inv3_edge_virtuals_are_provided(c):
    """3. A child provides the virtuals on its in-edges."""
    for n in all_nodes(c.out):
        for e in n.edges_to_dependencies():
            missing = set(e.virtuals) - {v.name for v in e.spec.provided_virtuals}
            if missing:
                yield f"{n.name} -> {e.spec.name} as {sorted(missing)}"


def inv4_build_spec_is_an_input(c):
    """4. The build spec of a spliced node is an input node (or an input's build spec)."""
    for n in all_nodes(c.out):
        if n.spliced and c.h(n.build_spec) not in c.input_hashes:
            yield f"build spec of {n.name} is not an input node"


def inv5_spliced_differs_only_in_dependencies(c):
    """5. A spliced node and its build spec agree on everything but dependencies."""
    strip = ("dependencies", "hash", "build_spec")
    for n in all_nodes(c.out):
        if n.spliced:
            a = {k: v for k, v in n.to_node_dict().items() if k not in strip}
            b = {k: v for k, v in n.build_spec.to_node_dict().items() if k not in strip}
            if a != b:
                keys = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
                yield f"{n.name} differs from its build spec in {keys}"


def inv6_spliced_nodes_have_no_build_deps(c):
    """6. Build dependencies of a spliced node live on its build spec only."""
    for n in all_nodes(c.out):
        if n.spliced and n.edges_to_dependencies(depflag=dt.BUILD):
            yield f"spliced {n.name} has build edges"


def inv7_spliced_iff_dependencies_changed(c):
    """7. A spliced node has other link/run deps than its build spec; others are input nodes."""
    for n in all_nodes(c.out):
        if n.spliced:
            mine = sorted(c.h(e.spec) for e in n.edges_to_dependencies(depflag=NON_BUILD))
            built = sorted(
                c.h(e.spec) for e in n.build_spec.edges_to_dependencies(depflag=NON_BUILD)
            )
            if mine == built:
                yield f"{n.name} is spliced but has the dependencies of its build spec"
        elif c.h(n) not in c.input_hashes:
            yield f"{n.name} is not spliced but is not an input node either"


def inv8_cached_hashes_are_correct(c):
    """8. Cached hashes equal hashes recomputed from the graph."""
    for n in all_nodes(c.out):
        if n.dag_hash() != c.h(n):
            yield f"{n.name}: cached {n.dag_hash()[:7]}, recomputed {c.h(n)[:7]}"


def edge_images(c, p, e) -> list:
    """Edges of output p that input edge e went to: the replacement if e went to a target,
    else same child name, or shared virtuals (an edge can be split by virtual)"""
    edges = p.edges_to_dependencies(depflag=NON_BUILD)
    if c.h(e.spec) in c.target_hashes:
        out = [f for f in edges if c.stands_for(f.spec, c.replacement)]
        if out:
            return out
    return [
        f
        for f in edges
        if (set(f.virtuals) & set(e.virtuals) if e.virtuals else f.spec.name == e.spec.name)
    ]


def edge_problems(c, p, x):
    """Why the link/run edges of input x do not map to those of output p. Input edges that are
    redirected to the same node merge; an edge can be split by virtual."""
    pre: Dict[int, list] = collections.defaultdict(list)
    for e in x.edges_to_dependencies(depflag=NON_BUILD):
        fs = edge_images(c, p, e)
        if not fs:
            yield f"edge {p.name} -> {e.spec.name} of the input is gone"
        for f in fs:
            pre[id(f)].append(e)
        for v in e.virtuals:
            n = sum(v in f.virtuals for f in fs)
            if n != 1:
                yield f"virtual {v} of {p.name} -> {e.spec.name} is on {n} edges"
    for f in p.edges_to_dependencies(depflag=NON_BUILD):
        es = pre.get(id(f))
        if not es:
            yield f"edge {p.name} -> {f.spec.name} comes from nowhere"
            continue
        flags = dt.NONE
        for e in es:
            flags |= e.depflag & NON_BUILD
        if f.depflag != flags:
            yield (
                f"edges {p.name} -> {[e.spec.name for e in es]} {dt.flag_to_tuple(flags)} "
                f"became {dt.flag_to_tuple(f.depflag)}"
            )
        if not set(f.virtuals) <= {v for e in es for v in e.virtuals}:
            yield f"edge {p.name} -> {f.spec.name} gained virtuals {f.virtuals}"


def inv10_edges_are_preserved(c):
    """10. Every link/run edge of the input of a changed node maps to an edge of the node."""
    for p in all_nodes(c.out):
        if not p.spliced or c.h(p) in c.input_hashes:
            continue
        origins = c.origins(p)
        if not origins:
            yield f"{p.name} stands for no input node"
            continue
        problems = [list(edge_problems(c, p, x)) for x in origins]
        if all(problems):
            yield from problems[0]


def inv11_only_target_and_clashes_are_replaced(c):
    """11. A dependency is replaced only if it is a target, or clashes with the winning side."""
    winner = c.replacement_hashes if c.transitive else c.spec_hashes
    for p in all_nodes(c.out):
        if not p.spliced or c.h(p) in c.input_hashes:
            continue
        for x in c.origins(p)[:1]:
            for e, f in (
                (e, f)
                for e in x.edges_to_dependencies(depflag=NON_BUILD)
                for f in edge_images(c, p, e)
            ):
                old, new = e.spec, f.spec
                if c.h(new) == c.h(old) or c.stands_for(new, old):
                    continue
                if c.h(old) in c.target_hashes and c.stands_for(new, c.replacement):
                    continue
                clash = new.name == old.name or bool(e.virtuals)
                if clash and any(c.h(o) in winner for o in c.origins(new)):
                    continue
                yield f"{p.name} -> {old.name}@{old.version} replaced by {new.name}@{new.version}"


def inv12_target_replaced(c):
    """12. The targets are gone from the link/run DAG, and the replacement is in it."""
    closure = link_run(c.out)
    for n in closure:
        if c.h(n) in c.target_hashes and c.h(n) not in c.replacement_dag_hashes:
            yield f"target {n.name}@{n.version} is still there"
    if not any(c.stands_for(n, c.replacement) for n in closure):
        yield f"replacement {c.replacement.name}@{c.replacement.version} is missing"


def inv14_root_stands_for_spec(c):
    """14. The result stands for the spec's root, or is the replacement if the root is a target."""
    if c.h(c.spec) in c.target_hashes:
        if c.h(c.out) != c.h(c.replacement):
            yield "root is a target but the result is not the replacement"
    elif c.out.name != c.spec.name or c.h(c.out.build_spec) != c.h(c.spec.build_spec):
        yield f"result root {c.out.name} does not stand for spec root {c.spec.name}"


def inv15_json_round_trip(c):
    """15. The result survives a JSON round trip: hashes, spliced flags, build specs."""
    back = Spec.from_json(c.out.to_json())
    a = {n.dag_hash(): (n.spliced, n.build_spec.dag_hash()) for n in all_nodes(c.out)}
    b = {n.dag_hash(): (n.spliced, n.build_spec.dag_hash()) for n in all_nodes(back)}
    if a != b:
        yield f"round trip differs in {len(set(a.items()) ^ set(b.items()))} nodes"
    if back.dag_hash() != c.out.dag_hash():
        yield "root hash differs after round trip"


def inv21_dependencies_map_onto(c):
    """21. The link/run edges of a spliced node's build spec map onto its own: each to exactly
    one edge, by virtual or else by name, and every edge is the image of one. Relocation needs
    this map: prefixes in the binary are those of the build spec's dependencies."""
    for n in all_nodes(c.out):
        if not n.spliced:
            continue
        mine = n.edges_to_dependencies(depflag=NON_BUILD)
        hit: Set[int] = set()
        for e in n.build_spec.edges_to_dependencies(depflag=NON_BUILD):
            images = [
                f
                for f in mine
                if (
                    set(f.virtuals) & set(e.virtuals) if e.virtuals else f.spec.name == e.spec.name
                )
            ]
            hit.update(id(f) for f in images)
            if len(images) != 1:
                to = ", ".join(f.spec.name for f in images) or "nothing"
                yield f"{n.name} -> {e.spec.name} of the build spec maps to {to}"
        for f in mine:
            if id(f) not in hit:
                yield f"{n.name} -> {f.spec.name} is the image of no edge of the build spec"


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
    inv14_root_stands_for_spec,
    inv15_json_round_trip,
    inv21_dependencies_map_onto,
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


MAYBE = "maybe: "


def impossible(spec, targets, replacement, transitive) -> Optional[str]:
    """Why no valid splice exists, under the semantics of the rewrite. Reasons starting with
    MAYBE allow both outcomes: whether a clash with an ancestor of a target makes the splice
    impossible depends on whether the clashing node survives."""
    th = {t.dag_hash() for t in targets}
    if spec.dag_hash() in th:
        return None
    if not targets:
        return "nothing to replace"
    repl_dag = link_run(replacement)
    repl_used = set().union(*(used_as(replacement, n) for n in repl_dag))
    surviving = list(
        spec.traverse(deptype=NON_BUILD, key=spack.traverse.by_dag_hash, visited=set(th))
    )
    surv_h = {n.dag_hash() for n in surviving}
    ancestors = [a for a in ancestors_of(spec, targets) if a.dag_hash() in surv_h]
    names = {a.name for a in ancestors if a is not spec or not transitive} & {
        n.name for n in repl_dag[1:] if n.dag_hash() not in surv_h
    }
    virt = set().union(*(used_as(spec, a) for a in ancestors)) & repl_used
    if names or virt:
        return f"{MAYBE}the replacement uses {sorted(names | virt)} of an ancestor of a target"
    taken = set().union(*(used_as(spec, t) for t in targets)) & repl_used
    if taken:  # unless the node using it loses a clash and is dropped
        return f"{MAYBE}the replacement's DAG provides {sorted(taken)}, which a target is used as"
    missing = set().union(*(used_as(spec, t) for t in targets)) - {
        v.name for v in replacement.provided_virtuals
    }
    if missing:
        return f"the replacement does not provide {sorted(missing)}"
    if transitive:
        if any(n.name == spec.name for n in repl_dag):
            return f"the replacement's DAG has a node named like the root {spec.name}"
        return None
    if any(
        n.name == replacement.name and n.dag_hash() != replacement.dag_hash() for n in surviving
    ):
        return f"a surviving node is named like the replacement {replacement.name}"
    return None


def node_in(out: Spec, c: Case, x: Spec) -> Optional[Spec]:
    for n in link_run(out):
        if c.stands_for(n, x):
            return n
    return None


def not_relocatable(spec, targets, replacement, transitive) -> bool:
    """Whether the splice, without the relocation check of Spec.splice, fails invariant 21"""
    splice = getattr(spack.spec, "_Splice", None)
    if splice is None or not hasattr(splice, "_validate_relocation"):
        return False
    validate = splice._validate_relocation
    splice._validate_relocation = lambda *args: None
    try:
        out = do_splice(spec, targets, replacement, transitive)
    except spack.spec.SpliceError:
        return False
    finally:
        splice._validate_relocation = validate
    return any(inv21_dependencies_map_onto(Case(spec, targets, replacement, transitive, out)))


def check(spec, targets, replacement, transitive) -> Dict[str, List[str]]:
    """Splice and check every invariant, plus 9 (inputs unchanged), 13 (determinism),
    16 (idempotence), 17 (undo) and 19 (edge order)"""
    before = fingerprint(spec), fingerprint(replacement)
    reason = impossible(spec, targets, replacement, transitive)
    try:
        out = do_splice(spec, targets, replacement, transitive)
    except spack.spec.SpliceError as e:
        if reason or not_relocatable(spec, targets, replacement, transitive):
            return {}
        return {"splice raised SpliceError": [str(e)]}
    except Exception as e:
        return {"splice crashed": [f"{type(e).__name__}: {e}"[:200]]}
    if reason and not reason.startswith(MAYBE):
        cyclic = " (cyclic)" if has_cycle(out) else ""
        return {"0. an impossible splice is rejected": [f"{reason}, but accepted{cyclic}"]}
    if has_cycle(out):
        return {"0. the result is acyclic": ["cyclic"]}
    v: Dict[str, List[str]] = {}
    if (fingerprint(spec), fingerprint(replacement)) != before:
        v["9. the inputs are unchanged"] = ["spec or replacement changed"]
    c = Case(spec, targets, replacement, transitive, out)
    for inv in INVARIANTS:
        try:
            found = list(inv(c))
        except Exception as e:
            found = [f"check crashed: {type(e).__name__}: {e}"[:200]]
        if found:
            v[inv.__doc__.splitlines()[0]] = found
    again = do_splice(spec, targets, replacement, transitive)
    if recomputed_hashes(again)[id(again)] != c.h(out):
        v["13. splicing is deterministic"] = ["second splice differs"]

    if c.h(spec) in c.target_hashes:
        return v

    # 16. idempotence: splicing the replacement again changes nothing
    r_out = node_in(out, c, replacement)
    if r_out is not None:
        try:
            twice = splice_at(out, replacement, transitive, r_out)
            if twice.dag_hash() != out.dag_hash():
                v["16. splicing twice is splicing once"] = [describe_diff(out, twice)]
        except spack.spec.SpliceError as e:
            v["16. splicing twice is splicing once"] = [f"second splice raised: {e}"]

    # 17. undo, for a single target in intransitive mode
    if not transitive and len(targets) == 1 and r_out is not None:
        try:
            undone = splice_at(out, targets[0], False, r_out)
            if undone.dag_hash() != spec.dag_hash():
                v["17. splicing the target back undoes the splice"] = [describe_diff(spec, undone)]
        except spack.spec.SpliceError as e:
            v["17. splicing the target back undoes the splice"] = [f"raised: {e}"]

    # 19. edge insertion order does not matter
    try:
        rs, rr = reversed_copy(spec), reversed_copy(replacement)
        rt = [n for t in targets for n in rs.traverse() if n.dag_hash() == t.dag_hash()][:1]
        out2 = do_splice(rs, rt or targets, rr, transitive)
        if out2.dag_hash() != out.dag_hash():
            v["19. edge order does not matter"] = ["different result"]
    except spack.spec.SpliceError as e:
        v["19. edge order does not matter"] = [f"raised: {e}"]
    return v


def check_modes_agree(spec, targets, replacement) -> Dict[str, List[str]]:
    """18. Without clashes, transitive and intransitive splices agree"""
    th = {t.dag_hash() for t in targets}
    if spec.dag_hash() in th:
        return {}
    surviving = list(
        spec.traverse(deptype=NON_BUILD, key=spack.traverse.by_dag_hash, visited=set(th))
    )
    names = {n.name: n.dag_hash() for n in surviving}
    virt = {v for n in surviving for v in used_as(spec, n)}
    for n in link_run(replacement):
        if n.name in names and names[n.name] != n.dag_hash():
            return {}
        if used_as(replacement, n) & virt:
            return {}
    results = []
    for transitive in (True, False):
        try:
            results.append(do_splice(spec, targets, replacement, transitive).dag_hash())
        except spack.spec.SpliceError as e:
            results.append(f"SpliceError: {e}")
    if results[0] != results[1]:
        return {
            "18. without clashes, both modes agree": [f"{results[0][:60]} vs {results[1][:60]}"]
        }
    return {}


def check_commutes(rng: random.Random) -> Dict[str, List[str]]:
    """20. Two splices of unrelated targets with disjoint replacements commute"""
    pool = Pool()
    spec = random_dag(rng, pool, rng.choice(LINK_PACKAGES), rng.choice(VERSIONS))
    cands = list(spec.traverse(root=False, deptype=NON_BUILD, key=id))
    if len(cands) < 2:
        return {}
    t1, t2 = rng.sample(cands, 2)

    def reach(a, b):
        return any(x is b for x in a.traverse(deptype=NON_BUILD, key=id))

    if reach(t1, t2) or reach(t2, t1):
        return {}
    r1 = pick_replacement(rng, pool, spec, t1)
    r2 = pick_replacement(rng, pool, spec, t2)
    n1 = {n.name for n in link_run(r1)} | {t1.name}
    n2 = {n.name for n in link_run(r2)} | {t2.name}
    if n1 & n2:
        return {}
    # nor may a replacement clash with the other target's DAG, which the first splice may drop
    if {n.name for n in link_run(r1)} & {n.name for n in link_run(t2)}:
        return {}
    if {n.name for n in link_run(r2)} & {n.name for n in link_run(t1)}:
        return {}
    # nor may both replacements bring providers of the same virtual
    v1 = {v for n in link_run(r1) for v in used_as(r1, n)} | used_as(spec, t1)
    v1 |= {v for n in link_run(t1) for v in used_as(t1, n)}
    v2 = {v for n in link_run(r2) for v in used_as(r2, n)} | used_as(spec, t2)
    v2 |= {v for n in link_run(t2) for v in used_as(t2, n)}
    if v1 & v2:
        return {}
    transitive = rng.random() < 0.5

    def two(first, second):
        (ta, ra), (tb, rb) = first, second
        mid = spec.splice(ra, transitive=transitive, target=ta)
        # the second target, possibly rewired by the first splice
        tb2 = next(
            (
                n
                for n in mid.traverse(deptype=NON_BUILD)
                if n.dag_hash() == tb.dag_hash()
                or (n.name == tb.name and n.build_spec.dag_hash() == tb.build_spec.dag_hash())
            ),
            None,
        )
        if tb2 is None:
            return None
        return mid.splice(rb, transitive=transitive, target=tb2).dag_hash()

    try:
        a = two((t1, r1), (t2, r2))
        b = two((t2, r2), (t1, r1))
    except spack.spec.SpliceError:
        return {}
    if a and b and a != b:
        return {"20. independent splices commute": [f"transitive={transitive}"]}
    return {}


# ---------------------------------------------------------------------------------------------
# Shrinking
# ---------------------------------------------------------------------------------------------

#: A DAG as data: nodes (name, version, variant, external) with the root first, and edges
#: (parent, child, depflag, virtuals) between node indices
Graph = Tuple[Tuple[Tuple[str, str, str, bool], ...], Tuple[Tuple[int, int, int, tuple], ...]]


def to_graph(spec: Spec) -> Tuple[Graph, List[Spec]]:
    order = list(spec.traverse(key=id))
    index = {id(n): i for i, n in enumerate(order)}
    nodes = tuple((n.name, str(n.version), str(n.variants), n.external) for n in order)
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
            name, version, variant, external = nodes[i]
            node_edges = [(make(c), f, v) for c, f, v in children[i]]
            built[i] = pool.node(name, version, node_edges, variant, external)
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
        simpler += [b for b in (dt.LINK, dt.RUN, dt.TEST) if f & b and f != b]
        simpler = [(p, c, g, v) for g in simpler]
        simpler += [(p, c, f, tuple(x for x in v if x != y)) for y in v]
        for e in simpler:
            yield nodes, edges[:k] + (e,) + edges[k + 1 :]


def shrink(spec: Spec, targets: List[Spec], replacement: Spec, transitive: bool, invariant: str):
    """Greedily remove parts of the case while the given invariant still fails. Spliced inputs
    are not graphs of plain nodes, so those cases are returned as they are."""
    if any(n.spliced for root in (spec, replacement) for n in root.traverse()):
        print("  spliced inputs, not shrinking")
        return spec, targets, replacement

    def materialize(g_spec, g_replacement, target_index):
        pool = Pool()
        spec_nodes, replacement_nodes = build(pool, g_spec), build(pool, g_replacement)
        root, repl = spec_nodes[0], replacement_nodes[0]
        if not EXPLICIT:
            return root, guessed_targets(root, repl), repl
        target = spec_nodes[target_index]
        if target is None or not any(n is target for n in link_run(root)):
            return None
        return root, [target], repl

    def fails(case) -> bool:
        return case is not None and any(k.startswith(invariant) for k in check(*case, transitive))

    def normalize(case):
        (g_spec, order), (g_replacement, _) = to_graph(case[0]), to_graph(case[2])
        return g_spec, g_replacement, next(i for i, n in enumerate(order) if n is case[1][0])

    if not fails((spec, targets, replacement)):
        print("  the case does not fail the invariant")
        return spec, targets, replacement
    state = normalize((spec, targets, replacement))
    progress = True
    while progress:
        progress = False
        g_spec, g_replacement, t = state
        candidates = [(g, g_replacement, t) for g in smaller(g_spec)]
        candidates += [(g_spec, g, t) for g in smaller(g_replacement)]
        for candidate in candidates:
            case = materialize(*candidate)
            if case is not None and case[1] and fails(case):
                state, progress = normalize(case), True
                break
    return materialize(*state)


# ---------------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------------


def describe_diff(a: Spec, b: Spec) -> str:
    da = {(n.name, str(n.version), n.spliced, n.dag_hash()[:5]) for n in a.traverse()}
    db = {(n.name, str(n.version), n.spliced, n.dag_hash()[:5]) for n in b.traverse()}
    return f"only in expected {sorted(da - db)}; only in got {sorted(db - da)}"


def describe(spec: Spec) -> str:
    cyclic = has_cycle(spec)
    label = lambda n: (
        f"{n.name}@{n.version}{n.variants}{' ext' if n.external else ''}/"
        + (f"#{id(n) % 1000:03d}" if cyclic else n.dag_hash()[:5])
    )
    lines, seen, stack = [], set(), [spec]
    while stack:
        n = stack.pop(0)
        if id(n) in seen:
            continue
        seen.add(id(n))
        tag = (
            f" (spliced from {n.build_spec.name}@{n.build_spec.version}/"
            f"{n.build_spec.dag_hash()[:5]})"
            if n.spliced
            else ""
        )
        lines.append(f"    {label(n)}{tag}")
        for e in n.edges_to_dependencies():
            v = f" as {','.join(e.virtuals)}" if e.virtuals else ""
            lines.append(f"      -> {label(e.spec)} [{','.join(dt.flag_to_tuple(e.depflag))}]{v}")
            stack.append(e.spec)
    return "\n".join(lines)


def show(spec, targets, replacement, transitive):
    print(f"  targets {[f'{t.name}@{t.version}/{t.dag_hash()[:5]}' for t in targets]}")
    print(f"  spec\n{describe(spec)}\n  replacement\n{describe(replacement)}")
    try:
        print(f"  result\n{describe(do_splice(spec, targets, replacement, transitive))}")
    except Exception as e:
        print(f"  result: {type(e).__name__}: {e}")


def run_seeds(chunk):
    """Check the cases of some seeds; returns the number of splices and the failures"""
    seeds, explicit, modes = chunk
    global EXPLICIT
    EXPLICIT = explicit
    checked, found = 0, []
    for seed in seeds:
        case = random_case(random.Random(seed), EXPLICIT)
        if case is not None:
            spec, targets, replacement = case
            size = len(all_nodes(spec)) + len(all_nodes(replacement))
            for transitive in modes:
                checked += 1
                for inv, msgs in check(spec, targets, replacement, transitive).items():
                    found.append((inv, (size, seed, transitive, msgs)))
            for inv, msgs in check_modes_agree(spec, targets, replacement).items():
                found.append((inv, (size, seed, None, msgs)))
        if EXPLICIT:
            for inv, msgs in check_commutes(random.Random(-seed - 1)).items():
                found.append((inv, (0, seed, None, msgs)))
    return checked, found


def main():
    global EXPLICIT
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--transitive", choices=("yes", "no", "both"), default="both")
    parser.add_argument("--show", type=int, default=1, help="examples to print per invariant")
    parser.add_argument("--only", help="print examples only of invariants starting with this")
    parser.add_argument("--guess", action="store_true", help="let Spec.splice guess the target")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--shrink", metavar="INVARIANT", help="minimize --seed for this invariant")
    args = parser.parse_args()
    EXPLICIT = HAS_TARGET and not args.guess
    modes = {"yes": [True], "no": [False], "both": [True, False]}[args.transitive]

    if args.shrink:
        with spack.repo.use_repositories():
            case = random_case(random.Random(args.seed), EXPLICIT)
            if case is None:
                print(f"seed {args.seed} has no case")
                return
            spec, targets, replacement = shrink(*case, modes[0], args.shrink)
            print(f"seed {args.seed}, transitive={modes[0]}")
            show(spec, targets, replacement, modes[0])
            for inv, msgs in check(spec, targets, replacement, modes[0]).items():
                print(f"  ! {inv[:60]}: {msgs[0]}")
        return

    failures = collections.defaultdict(list)  # invariant -> [(size, seed, transitive, msgs)]
    checked = 0
    seeds = list(range(args.seed, args.seed + args.cases))
    chunks = [(seeds[i :: args.jobs], EXPLICIT, modes) for i in range(args.jobs)]
    with spack.repo.use_repositories():
        if args.jobs == 1:
            results = [run_seeds(chunks[0])]
        else:
            # fork: workers inherit the repository context and the globals
            with multiprocessing.get_context("fork").Pool(args.jobs) as pool:
                results = pool.map(run_seeds, chunks)
        for n, found in results:
            checked += n
            for inv, item in found:
                failures[inv].append(item)

        print(f"{checked} splices checked\n")
        for inv, cases in sorted(failures.items()):
            print(f"{len(cases):5d}  {inv}")
        for inv, cases in sorted(failures.items()):
            if args.only and not inv.startswith(args.only):
                continue
            for size, seed, transitive, msgs in sorted(cases, key=lambda x: x[:2])[: args.show]:
                print(f"\n=== {inv}\nseed {seed}, transitive={transitive}")
                case = random_case(random.Random(seed), EXPLICIT)
                if case is not None and transitive is not None:
                    show(*case, transitive)
                for m in msgs[:5]:
                    print(f"  ! {m}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
