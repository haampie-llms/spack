"""Mutation-based fuzzer for Spack concretization error messages.

Run with:  spack python fuzz.py --seed N --n COUNT --out FILE.jsonl [--full]

Each case starts from a real package, injects one known-bad thing (the "mutation"),
records the tokens an accurate error message would have to mention, runs the solver
in-process, and records what came out.  Without --full the second-pass causation
solve is skipped for speed; the numbered messages of the first pass are kept.
"""

import argparse
import collections
import json
import random
import sys
import time
import traceback

import spack.concretize
import spack.config
import spack.error
import spack.repo
import spack.solver.asp as asp
import spack.spec
import spack.util.tty as tty
import spack.variant
import spack.version
from spack.spec import Spec

tty.set_verbose(False)
tty.set_debug(False)

# ----------------------------------------------------------------------------
# solver plumbing
# ----------------------------------------------------------------------------


def fast_raise_if_errors(self):
    """Replacement for ErrorHandler.raise_if_errors that skips the causation pass."""
    initial = asp.extract_args(self.model, "error")
    if not initial:
        return
    header = "failed to concretize for the following reasons:"
    msgs = self.error_messages(initial)
    err = asp.UnsatisfiableSpecError(f"{header}\n{self._numbered(msgs)}")
    err.printed = True
    raise err


def solve(specs, config, full):
    """Return (outcome, message, seconds)."""
    if not full:
        asp.ErrorHandler.raise_if_errors = fast_raise_if_errors
    scope = spack.config.InternalConfigScope("fuzz", config or {})
    t0 = time.time()
    try:
        with spack.config.CONFIG.override(scope):
            if len(specs) == 1:
                spack.concretize.concretize_one(Spec(specs[0]))
            else:
                asp.Solver().solve([Spec(s) for s in specs])
        return "sat", "", time.time() - t0
    except asp.UnsatisfiableSpecError as e:
        return "unsat", str(e), time.time() - t0
    except spack.error.SpackError as e:
        msg = str(e)
        if "internal error" in msg or "bug report" in msg:
            return "internal", msg, time.time() - t0
        if "taking more than" in msg:
            return "timeout", msg, time.time() - t0
        return "presolver", f"{type(e).__name__}: {msg}", time.time() - t0
    except Exception as e:  # noqa: BLE001
        return (
            "exception",
            f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}",
            time.time() - t0,
        )


# ----------------------------------------------------------------------------
# package introspection helpers
# ----------------------------------------------------------------------------

PATH = spack.repo.PATH
HOST_BAD_TARGETS = ["x86_64", "zen4", "skylake", "power9le", "riscv64"]
HOST_BAD_OS = ["ubuntu22.04", "rhel8", "windows10", "ventura"]
HOST_BAD_PLATFORMS = ["linux", "windows", "freebsd"]


def declared_versions(cls):
    out = []
    for v, attrs in cls.versions.items():
        if attrs.get("deprecated"):
            continue
        if any(k in attrs for k in ("branch", "tag", "commit")) and "sha256" not in attrs:
            continue
        out.append(v)
    return out


def deprecated_versions(cls):
    return [v for v, a in cls.versions.items() if a.get("deprecated")]


def is_virtual(name):
    try:
        return PATH.is_virtual(name)
    except Exception:  # noqa: BLE001
        return False


def providers(virtual):
    try:
        return sorted({s.name for s in PATH.providers_for(virtual)})
    except Exception:  # noqa: BLE001
        return []


def version_not_satisfying(cls, constraint_spec, exclude=()):
    """A declared version of cls that does not satisfy constraint_spec's version range."""
    vs = [v for v in declared_versions(cls) if v not in exclude]
    random.shuffle(vs)
    for v in vs:
        if not Spec(f"{cls.name}@={v}").satisfies(constraint_spec):
            return v
    return None


def version_satisfying(cls, when):
    vs = declared_versions(cls)
    random.shuffle(vs)
    for v in vs:
        if Spec(f"{cls.name}@={v}").satisfies(when):
            return v
    return None


def merge(*specs):
    s = Spec()
    for x in specs:
        s.constrain(Spec(str(x)))
    return s


def variant_values(var):
    """Declared values of a variant. Some are declared without `values=`, and then it is None."""
    return tuple(var.values or ())


def bool_variants(cls):
    out = []
    for when, vs in cls.variant_items():
        for n, v in vs.items():
            if set(variant_values(v)) == {True, False}:
                out.append((when, n, v))
    return out


# ----------------------------------------------------------------------------
# mutators: each returns dict(kind, specs, config, expect) or None if not applicable
# ----------------------------------------------------------------------------


def m_cond_variant(cls):
    cands = [
        (w, n, v)
        for w, vs in cls.variant_items()
        for n, v in vs.items()
        if str(w) and w.versions != spack.version.any_version
    ]
    random.shuffle(cands)
    for when, name, var in cands:
        v = version_not_satisfying(cls, when)
        if v is None:
            continue
        if set(variant_values(var)) == {True, False}:
            val = f"+{name}" if var.default in (False, "False") else f"~{name}"
        else:
            vals = [
                x for x in variant_values(var) if not isinstance(x, spack.variant.ConditionalValue)
            ]
            if not vals:
                continue
            val = f"{name}={random.choice(vals)}"
        return dict(
            kind="cond_variant",
            specs=[f"{cls.name}@={v} {val}"],
            config=None,
            expect=[name, str(when)],
        )
    return None


def m_cond_value(cls):
    for when, vs in cls.variant_items():
        for name, var in vs.items():
            cvals = [
                x
                for x in variant_values(var)
                if isinstance(x, spack.variant.ConditionalValue) and x.when
            ]
            if not cvals:
                continue
            cv = random.choice(cvals)
            v = version_not_satisfying(cls, cv.when)
            if v is None:
                continue
            return dict(
                kind="cond_value",
                specs=[f"{cls.name}@={v} {name}={cv.value}"],
                config=None,
                expect=[name, str(cv.value), str(cv.when)],
            )
    return None


def m_bad_value(cls):
    cands = [
        (n, v)
        for _, vs in cls.variant_items()
        for n, v in vs.items()
        if set(variant_values(v)) != {True, False} and n != "build_system"
    ]
    if not cands:
        return None
    n, v = random.choice(cands)
    return dict(
        kind="bad_variant_value", specs=[f"{cls.name} {n}=zzz"], config=None, expect=[n, "zzz"]
    )


def m_unknown_variant(cls):
    return dict(
        kind="unknown_variant",
        specs=[f"{cls.name}+nosuchvariant"],
        config=None,
        expect=["nosuchvariant"],
    )


def m_dep_version(cls):
    cands = []
    for when, deps in cls.dependencies.items():
        for dname, dep in deps.items():
            if dep.spec.versions != spack.version.any_version and not is_virtual(dname):
                cands.append((when, dname, dep))
    random.shuffle(cands)
    for when, dname, dep in cands:
        try:
            dcls = PATH.get_pkg_class(dname)
        except Exception:  # noqa: BLE001
            continue
        bad = version_not_satisfying(dcls, dep.spec)
        if bad is None:
            continue
        root = merge(cls.name, when) if str(when) else Spec(cls.name)
        try:
            rv = version_satisfying(cls, when) if str(when) else None
        except Exception:  # noqa: BLE001
            rv = None
        rs = str(root)
        if rv is not None and "@" not in rs:
            rs = f"{cls.name}@={rv} " + rs[len(cls.name) :]
        return dict(
            kind="dep_version_out_of_range",
            specs=[f"{rs} ^{dname}@={bad}"],
            config=None,
            expect=[dname, str(dep.spec.versions), str(bad)],
        )
    return None


def m_dep_condition_falsified(cls):
    """depends_on(x, when='+foo') and the user asks ~foo ^x."""
    cands = []
    for when, deps in cls.dependencies.items():
        if not str(when):
            continue
        for vname, vval in when.variants.items():
            if vval.value in (True, False):
                for dname in deps:
                    cands.append((when, vname, vval.value, dname))
    if not cands:
        return None
    when, vname, val, dname = random.choice(cands)
    flip = f"~{vname}" if val else f"+{vname}"
    if is_virtual(dname):
        provs = providers(dname)
        if not provs:
            return None
        dname_req = random.choice(provs)
    else:
        dname_req = dname
    return dict(
        kind="dep_condition_falsified",
        specs=[f"{cls.name} {flip} ^{dname_req}"],
        config=None,
        expect=[dname_req, vname],
    )


def m_not_a_dependency(cls):
    other = random.choice(["zlib-ng", "libpng", "libiconv", "readline", "gmp"])
    if other in cls.dependencies_by_name(when=False):
        return None
    return dict(
        kind="not_a_dependency", specs=[f"{cls.name} ^{other}"], config=None, expect=[other]
    )


def m_conflict(cls):
    cands = [(w, c, m) for w, lst in cls.conflicts.items() for c, m in lst]
    random.shuffle(cands)
    for when, cspec, msg in cands:
        try:
            root = merge(cls.name, when, cspec)
        except Exception:  # noqa: BLE001
            continue
        if root.name != cls.name:
            continue
        exp = [msg] if msg else [str(cspec)]
        return dict(kind="conflict_directive", specs=[str(root)], config=None, expect=exp)
    return None


def m_requires(cls):
    cands = [
        (w, specs, pol, msg) for w, lst in cls.requirements.items() for specs, pol, msg in lst
    ]
    random.shuffle(cands)
    for when, specs, pol, msg in cands:
        if pol != "one_of" or len(specs) != 1:
            continue
        req = specs[0]
        root = merge(cls.name, when) if str(when) else Spec(cls.name)
        bad = None
        if req.versions != spack.version.any_version and req.name in (None, cls.name):
            v = version_not_satisfying(cls, req)
            if v is not None:
                bad = f"@={v}"
        elif req.name in (None, cls.name) and req.variants:
            vn, vv = next(iter(req.variants.items()))
            if vv.value in (True, False):
                bad = f"~{vn}" if vv.value else f"+{vn}"
        if bad is None:
            continue
        try:
            root.constrain(Spec(bad))
        except Exception:  # noqa: BLE001
            continue
        return dict(
            kind="requires_directive",
            specs=[str(root)],
            config=None,
            expect=[msg] if msg else [str(req)],
        )
    return None


def m_deprecated(cls):
    vs = deprecated_versions(cls)
    if not vs:
        return None
    return dict(
        kind="deprecated_version",
        specs=[f"{cls.name}@={random.choice(vs)}"],
        config=None,
        expect=["deprecated"],
    )


def m_unknown_version(cls):
    return dict(
        kind="unknown_version", specs=[f"{cls.name}@=99.99.99"], config=None, expect=["99.99.99"]
    )


def m_arch(cls):
    k = random.choice(["target", "os", "platform"])
    val = random.choice(
        {"target": HOST_BAD_TARGETS, "os": HOST_BAD_OS, "platform": HOST_BAD_PLATFORMS}[k]
    )
    return dict(kind=f"arch_{k}", specs=[f"{cls.name} {k}={val}"], config=None, expect=[val])


def m_compiler(cls):
    choice = random.choice(
        [
            "%gcc@99",
            "%fortran=apple-clang",
            "%c=gcc",
            "%c=gcc %c=apple-clang",
            "%cxx=gcc",
            "%clang@99",
        ]
    )
    exp = {
        "%gcc@99": ["gcc@99"],
        "%fortran=apple-clang": ["fortran"],
        "%c=gcc": ["gcc"],
        "%c=gcc %c=apple-clang": ["gcc", "apple-clang"],
        "%cxx=gcc": ["gcc"],
        "%clang@99": ["clang@99"],
    }[choice]
    return dict(
        kind="compiler_" + choice.split("=")[0].strip("%").split("@")[0],
        specs=[f"{cls.name} {choice}"],
        config=None,
        expect=exp,
    )


def virtual_deps(cls):
    out = set()
    for when, deps in cls.dependencies.items():
        for dname in deps:
            if is_virtual(dname) and dname not in ("c", "cxx", "fortran"):
                out.add((str(when), dname))
    return sorted(out)


def m_two_providers(cls):
    vd = virtual_deps(cls)
    random.shuffle(vd)
    for when, virt in vd:
        provs = providers(virt)
        if len(provs) < 2:
            continue
        a, b = random.sample(provs, 2)
        root = merge(cls.name, when) if when else Spec(cls.name)
        return dict(
            kind="two_providers", specs=[f"{root} ^{a} ^{b}"], config=None, expect=[virt, a, b]
        )
    return None


def m_cfg_buildable(cls):
    deps = [d for d in cls.dependencies_by_name(when=False) if not is_virtual(d)]
    if not deps:
        return None
    d = random.choice(deps)
    return dict(
        kind="cfg_buildable_false",
        specs=[cls.name],
        config={"packages": {d: {"buildable": False}}},
        expect=[d, "buildable"],
    )


def m_cfg_virtual_buildable(cls):
    vd = virtual_deps(cls)
    if not vd:
        return None
    when, virt = random.choice(vd)
    root = merge(cls.name, when) if when else Spec(cls.name)
    return dict(
        kind="cfg_virtual_buildable_false",
        specs=[str(root)],
        config={"packages": {virt: {"buildable": False}}},
        expect=[virt, "buildable"],
    )


def m_cfg_require_variant(cls):
    bv = [(w, n, v) for w, n, v in bool_variants(cls) if not str(w)]
    if not bv:
        return None
    _, n, v = random.choice(bv)
    on = f"+{n}"
    off = f"~{n}"
    return dict(
        kind="cfg_require_variant_conflict",
        specs=[f"{cls.name} {on}"],
        config={"packages": {cls.name: {"require": off}}},
        expect=[n, "requirement"],
    )


def m_cfg_require_version(cls):
    vs = declared_versions(cls)
    if len(vs) < 2:
        return None
    a, b = random.sample(vs, 2)
    return dict(
        kind="cfg_require_version_conflict",
        specs=[f"{cls.name}@={a}"],
        config={"packages": {cls.name: {"require": f"@={b}"}}},
        expect=[str(b), "requirement"],
    )


def m_cfg_all_require(cls):
    r = random.choice(["%gcc@99", "target=x86_64", "@99.99", "os=ubuntu22.04", "^zlib-ng@99"])
    return dict(
        kind="cfg_all_require",
        specs=[cls.name],
        config={"packages": {"all": {"require": r}}},
        expect=[r.lstrip("%^@").split("@")[0] or r, "requirement"],
    )


def m_cfg_external_old(cls):
    cands = []
    for when, deps in cls.dependencies.items():
        for dname, dep in deps.items():
            if (
                dep.spec.versions != spack.version.any_version
                and not is_virtual(dname)
                and not str(when)
            ):
                cands.append((dname, dep))
    random.shuffle(cands)
    for dname, dep in cands:
        try:
            dcls = PATH.get_pkg_class(dname)
        except Exception:  # noqa: BLE001
            continue
        bad = version_not_satisfying(dcls, dep.spec)
        if bad is None:
            continue
        cfg = {
            "packages": {
                dname: {
                    "buildable": False,
                    "externals": [{"spec": f"{dname}@={bad}", "prefix": "/usr"}],
                }
            }
        }
        return dict(
            kind="cfg_external_too_old",
            specs=[cls.name],
            config=cfg,
            expect=[dname, str(bad), str(dep.spec.versions)],
        )
    return None


def m_cfg_provider_require(cls):
    vd = virtual_deps(cls)
    random.shuffle(vd)
    for when, virt in vd:
        provs = providers(virt)
        if len(provs) < 2:
            continue
        a, b = random.sample(provs, 2)
        root = merge(cls.name, when) if when else Spec(cls.name)
        return dict(
            kind="cfg_provider_require_conflict",
            specs=[f"{root} ^{a}"],
            config={"packages": {virt: {"require": b}}},
            expect=[virt, a, b],
        )
    return None


def m_env_unify(cls):
    cands = []
    for when, deps in cls.dependencies.items():
        for dname, dep in deps.items():
            if (
                dep.spec.versions != spack.version.any_version
                and not is_virtual(dname)
                and not str(when)
            ):
                cands.append((dname, dep))
    random.shuffle(cands)
    for dname, dep in cands:
        try:
            dcls = PATH.get_pkg_class(dname)
        except Exception:  # noqa: BLE001
            continue
        bad = version_not_satisfying(dcls, dep.spec)
        if bad is None:
            continue
        return dict(
            kind="env_unify_conflict",
            specs=[f"{dname}@={bad}", cls.name],
            config=None,
            expect=[dname, str(bad), str(dep.spec.versions)],
        )
    return None


def m_two_cond_deps(cls):
    by_dep = {}
    for when, deps in cls.dependencies.items():
        if not str(when) or not when.variants:
            continue
        for dname, dep in deps.items():
            if dep.spec.versions != spack.version.any_version:
                by_dep.setdefault(dname, []).append((when, dep))
    for dname, lst in by_dep.items():
        random.shuffle(lst)
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (w1, d1), (w2, d2) = lst[i], lst[j]
                if d1.spec.versions.intersects(d2.spec.versions):
                    continue
                try:
                    root = merge(cls.name, w1, w2)
                except Exception:  # noqa: BLE001
                    continue
                return dict(
                    kind="two_conditional_deps_disjoint",
                    specs=[str(root)],
                    config=None,
                    expect=[dname, str(d1.spec.versions), str(d2.spec.versions)],
                )
    return None


MUTATORS = [
    m_cond_variant,
    m_cond_value,
    m_bad_value,
    m_unknown_variant,
    m_dep_version,
    m_dep_condition_falsified,
    m_not_a_dependency,
    m_conflict,
    m_requires,
    m_deprecated,
    m_unknown_version,
    m_arch,
    m_compiler,
    m_two_providers,
    m_cfg_buildable,
    m_cfg_virtual_buildable,
    m_cfg_require_variant,
    m_cfg_require_version,
    m_cfg_all_require,
    m_cfg_external_old,
    m_cfg_provider_require,
    m_env_unify,
    m_two_cond_deps,
]


def pick_package(names, max_direct_deps, relax=False):
    while True:
        name = random.choice(names)
        if is_virtual(name):
            continue
        try:
            cls = PATH.get_pkg_class(name)
        except Exception:  # noqa: BLE001
            continue
        if not declared_versions(cls):
            continue
        ndeps = len(cls.dependencies_by_name(when=False))
        if not relax and ndeps > max_direct_deps and random.random() < 0.9:
            continue
        return cls


#: how many packages to try before giving up on a mutator for this round
TRIES_PER_MUTATOR = 60


def generate(names, muts, n, max_direct_deps):
    """Draw cases mutator-first, fewest-so-far first.

    Picking a package and then a mutator starves every mutator whose precondition is rare: a
    version-conditional variant exists on 348 of 9020 packages and a `requires()` directive on
    183, and `--max-deps` skips 90% of the packages that have them, so cond_variant, cond_value
    and requires_directive produced 0 of 440 cases while unknown_version and unknown_variant --
    which apply to everything -- produced a quarter of them.
    """
    cases, produced, budget = [], collections.Counter(), n * TRIES_PER_MUTATOR
    for m in muts:
        produced.setdefault(m.__name__, 0)
    while len(cases) < n and budget > 0:
        m = min(muts, key=lambda f: (produced[f.__name__], random.random()))
        for i in range(TRIES_PER_MUTATOR):
            if budget <= 0:
                break
            budget -= 1
            # a rare mutator is usually rare among small packages too, so stop honouring
            # --max-deps once the easy candidates are exhausted
            cls = pick_package(names, max_direct_deps, relax=i >= TRIES_PER_MUTATOR // 2)
            try:
                c = m(cls)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"mutator {m.__name__} failed on {cls.name}: {e}\n")
                continue
            if c:
                c["package"] = cls.name
                cases.append(c)
                break
        # count the round either way, so a mutator that cannot apply anywhere is not retried
        # forever at the expense of the others
        produced[m.__name__] += 1
    return cases


def mentions(message, expect):
    low = message.lower()
    return [t for t in expect if t and t.lower() in low]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", required=True)
    ap.add_argument("--full", action="store_true", help="run the causation pass too")
    ap.add_argument("--max-deps", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--kinds", default="", help="comma-separated mutator kinds to restrict to")
    ap.add_argument("--replay", default="", help="jsonl file of cases to rerun verbatim")
    args = ap.parse_args()
    random.seed(args.seed)

    spack.config.CONFIG.push_scope(
        spack.config.InternalConfigScope(
            "fuzzbase", {"concretizer": {"timeout": args.timeout, "error_on_timeout": True}}
        )
    )

    cases = []
    if args.replay:
        with open(args.replay) as f:
            cases = [json.loads(line) for line in f if line.strip()]
    else:
        names = PATH.all_package_names()
        muts = MUTATORS
        want = set(args.kinds.split(",")) if args.kinds else set()
        if want:
            # m_arch and m_compiler each emit several kinds ("arch_target", "compiler_c", ...),
            # so match a wanted kind against the function name in both directions and filter the
            # generated cases by their actual kind afterwards.
            muts = [
                m
                for m in MUTATORS
                if any(w == m.__name__[2:] or w.startswith(m.__name__[2:]) for w in want)
            ]
            if not muts:
                sys.exit(f"--kinds {args.kinds} matched no mutator")
        cases = generate(names, muts, args.n, args.max_deps)
        if want:
            cases = [
                c
                for c in cases
                if c["kind"] in want or c["kind"] in {m.__name__[2:] for m in muts}
            ]

    with open(args.out, "w") as out:
        for i, c in enumerate(cases):
            outcome, msg, dt = solve(c["specs"], c.get("config"), args.full)
            rec = dict(c)
            rec.update(
                outcome=outcome,
                message=msg,
                seconds=round(dt, 1),
                mentioned=mentions(msg, c["expect"]),
            )
            out.write(json.dumps(rec) + "\n")
            out.flush()
            sys.stderr.write(
                f"[{i + 1}/{len(cases)}] {c['kind']:32s} {c['package']:24s} {outcome:9s} {dt:5.1f}s\n"  # noqa: E501
            )


if __name__ == "__main__":
    main()
