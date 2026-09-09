# Concretization error messages: what is fixed, what is left

Findings from 62 hand-written cases and a 440-case fuzzed corpus run against the `builtin`
package repository on macOS arm64 (Darwin 24.6, `os=sequoia target=m4`) with apple-clang 17
and a Fortran-only gcc 14.2 registered as externals.

The first edition of this report ranked ten fixes. Ten commits have since landed on this
branch: three from `hs/fix/error-msgs-{1,2,3}` and seven written against the numbers below.
Every figure here is an A/B on the *same* 440 inputs, replayed with `fuzz.py --replay`.

## Where things stand

An error is scored on whether it names the thing the mutation actually broke. "any" means at
least one such token appears; "all" means every one does, which is the bar for acting on the
message without guessing.

| | any | all | internal errors |
|---|---|---|---|
| `develop` (b495c19f9e) | 75.5% | 58.1% | 14 |
| + `hs/fix/error-msgs-{1,2,3}` | 86.5% | 69.4% | 13 |
| + naming the unneeded edge, requirement provenance | 87.2% | 69.4% | 11 |
| + naming externals and requirements | **87.2%** | **78.3%** | **11** |

Measured over the 418 of 440 cases that never hit the solver timeout in any run. Timeouts are
wall-clock and this is a shared machine: two runs of *identical* code differed by 4%, and one
run under load lost five more cases to the timeout and 25% more solve time across every
bucket, including buckets these changes cannot touch. Solve-time claims below are therefore
restricted to back-to-back A/Bs of one bucket.

### Goal

> Every concretization error names both sides of the conflict and where each side came from;
> no input ever produces "Please submit a bug report".

Measurable as **any → 100%, all → ≥90%, internal errors → 0, worst fan-out → ≤10 lines.**

## What the three error-msgs branches fixed

37 of 437 comparable cases improved, none regressed.

**Conditional variants** (`error-msgs-3`) — the top finding of the first report. A user-set
variant that does not exist on the requested version was a hard constraint at
`concretize.lp:1618`, so the only feasible models changed the version and that is what got
blamed:

    $ spack spec hdf5@1.8.19+java
    -  1. Cannot satisfy 'hdf5@1.8.19' (version 1.14.6 does not match)
    +  1. Cannot set variant 'java' for 'hdf5@1.8.19': Package hdf5 has variant 'java' when @1.10:

This also turned one fuzzed internal error into a real message (`sundials cuda_arch=11,86`).

**Platform and target** (`error-msgs-2`), **os and providers** (`error-msgs-1`):

    -  1. Cannot select a single "node_platform" for package "zlib-ng"
    +  1. 'zlib-ng platform=linux' is not compatible with this machine (platform=darwin)

    -  1. Cannot select a single "node_os" for package "zlib-ng"
    +  1. Conflicting os values are required for package 'zlib-ng': 'sequoia' and 'ubuntu22.04'

`arch_os` went from 0/18 to 18/18, `arch_platform` from 2/19 to 19/19. The generic
`Cannot select a single "{attr}"` template, previously the single most common message in the
corpus at 74 cases, is gone.

## What was fixed here

**`solver: report unneeded dependency edges instead of failing silently`.** Asking for two
providers of one language left a dependency edge nothing required, tripping a hard constraint
at `concretize.lp:1034`. The solver had already derived the real explanation; the constraint
threw away the model carrying it:

    $ spack spec 'zlib-ng %c=gcc %c=apple-clang'
    -  ==> Error: Spack concretizer internal error. Please submit a bug report
    +  1. 'zlib-ng' cannot depend on 'gcc'
    +  2. zlib-ng cannot have a dependency on c
    +  3. Only external, or concrete, compilers are allowed for the c language

The weight matters: at anything below the requirement errors the solver *prefers* violating
this rule, which regressed `libelf %gcc` under `require: "%clang"` from the package's own
message to two lines about llvm. It is now above every other error, a last resort only.

**`solver: say which config file a requirement came from`.** The provenance existed and was
dropped one field short of the message — `_rules_from_requirements` already keeps the raw YAML
values, and `_mark_str` already formats a `file:line: ` prefix:

     3. Cannot satisfy 'hdf5@1.14.6' and 'hdf5@1.8.19'
    -   required because @1.14.6 is a requirement for package hdf5
    +   required because /home/user/.spack/packages.yaml:4: @1.14.6 is a requirement for package hdf5

No ASP changes; the text rides on facts that already exist. Requirements set through the API
carry no mark and get no prefix.

**`solver: quote the requirement that could not be satisfied`.** A `packages.yaml` requirement
reported only which package it affected, never which entry was at fault:

    -  1. cannot satisfy a requirement for package 'zlib-ng'.
    +  1. cannot satisfy requirement 'os=ubuntu22.04' from ~/.spack/packages.yaml:3 for package 'zlib-ng'

`cfg_all_require` went from 16/56 complete to 43/56, and its median message from 9 numbered
lines to 6.

**`solver: name the external on offer`.** `buildable: false` said only "no externals satisfy
the request"; all ten such cases in the corpus hid the cause:

    +  2. Cannot build cmake, since it is configured `buildable:false`; the external declared
    +     for it is 'cmake@3.30.0~ownlibs' from ~/.spack/packages.yaml:5
    +  3. ... and the external cmake@2.8.10.2 does not satisfy 'cmake@3.18:'
    +     required because hdf5 depends on cmake@3.18: when @1.14:

Two rules: one that names the external whatever the mismatch is, one that pairs it with a
version constraint it genuinely fails. The first attempt had only the pairing and skipped the
check that the version was at fault, which produced the false claim that `cmake@3.30.0` does
not satisfy `cmake@3.18:` when the real mismatch was `~ownlibs`. Versions are the common case,
but anything can enter a constraint. Both rules live in `error_messages.lp`, so they run on a
fixed model and cannot affect which model is chosen.

**Fuzzer sampling.** See "Corrections to the method" below.

## Corrections to the method

Three measurement bugs were found while doing the A/B. They matter because they all *flatter*
or *distort* the numbers above.

**1. The headline rates understate what users see.** `fuzz.py` disables the second-pass
causation solve by default, so a case costs one solve. But `spack spec` always runs it. For
`cfg_require_version_conflict` the difference is total:

    causation off:  0/23 name the conflicting requirement
    causation on:  22/23 name it, both sides, with the cause tree

So the worst-looking bucket in the per-kind table is not actually broken for users; its
*first-pass* message is just uninformative on its own. Any claim about a message being bad
must say which mode it was measured in.

**2. An internal error scored as a hit.** "Please submit a bug report" quotes the input spec
back, so substring matching found the expected tokens in it. Replacing one with a real message
therefore read as a regression — two `compiler_c` cases did exactly that. `analyze.py` now
counts an internal error as naming nothing, which is why the `develop` row above (75.5%) is
lower than the 78% quoted in the first edition.

**3. `analyze.py` read the wrong tree.** It scraped `error(...)` templates from a hardcoded
`~/spack` path and swallowed the `OSError`, so evaluating a fix in a worktree would have
silently bucketed every new message as `OTHER:`.

Also stale from the first edition: item 8 claimed every message is printed twice.
`raise_if_errors` has suppressed exact repeats since upstream #52191, which predates the
report. What remains is *near*-duplicates — `Cannot satisfy 'hdf5@1.8.19'` followed by the
same line with `(version 1.14.6 does not match)` appended — plus two chatter lines.

### The fuzzer was not testing what it claimed

Three of the 23 mutators produced **zero** of the 440 cases: `cond_variant`, `cond_value`,
`requires_directive`. The generator picked a package uniformly from all 9020 builtin packages
and then applied a random mutator, so a mutator fired only as often as its precondition held:

| precondition | packages | share |
|---|---|---|
| any package (`unknown_version`, `unknown_variant`, `arch_*`) | 9020 | 100% |
| has `conflicts()` | 2819 | 31% |
| **has a version-conditional variant** (`cond_variant`) | **348** | **3.9%** |
| has `requires()` (`requires_directive`) | 183 | 2.0% |
| has a `ConditionalValue` (`cond_value`) | 146 | 1.6% |

`--max-deps 5` compounded it: 329 of those 348 conditional-variant packages have more than
five direct dependencies and were skipped 90% of the time. Four mutators produced 48% of the
corpus, and `cond_variant` — the one that exercises the report's own top finding — never ran.

Cases are now drawn mutator-first, fewest-so-far first, searching up to 60 packages per
mutator and ignoring `--max-deps` for the second half of that search. A 120-case run covers 26
kinds at 1–6 cases each. Two mutators were added for classes nothing reached
(`unrelated_dep_no_compiler`, `propagated_variant_conflict`), and a crash was fixed: variants
declared without `values=` have `values` None, which killed three mutators on every such
package.

`requires_directive` is still under-sampled, and the cause is now known: 169 packages have a
`one_of` requirement with exactly one spec, but the mutator only knows how to break version and
boolean-variant requirements, not the common `requires("%gcc")` form.

## What is left, ranked

**1. Fan-out on `all:` requirements (up to 127 lines).** Each line now names the requirement,
but an `all:` requirement still produces one per package in the DAG, all identical. Collapsing
them changes how many error atoms a model carries, and so which model the optimizer picks, so
it needs its own before/after on the corpus rather than a local test. *Cost: moderate, and the
risk is in the optimization, not in the rule.*

**2. The remaining 11 internal errors.** Instrumenting integrity constraints — rewriting each
bare `:-` head into a tagged `error(...)` — identifies the blocker directly, and found
`concretize.lp:1034` for the compiler case. Two caveats: the pure well-formedness constraints
(`concretize.lp:81-97`) must stay hard, or the solver dodges everything else by dropping a
node; and every relaxation makes the program harder to solve, which bounds how much can be
relaxed at once.

The largest remaining class is `X ^dep` where the root has no compiler dependencies
(`maven ^zlib-ng`, `apktool ^libpng`; 5 of the 11). It resisted the technique entirely, and
the negative results are worth recording:

- **Not an integrity constraint.** All 62 in `concretize.lp` plus the 5 in
  `direct_dependency.lp`, `libc_compatibility.lp` and `os_compatibility.lp` were relaxed at
  once. Still no model.
- **Not the acyclicity extension.** Disabling `#edge` at `concretize.lp:2221` changes nothing.
- **Not reachable by a pre-solve check.** `zlib-ng` is in maven's optimistic possible-dependency
  closure — 607 packages, via openjdk — so no closure-based check can reject `maven ^zlib-ng`
  without also rejecting legitimate transitive `^` specs.
- The rule that *should* cover it, `error(10, "'{0}' is not a valid dependency for any package
  in the DAG")` at `concretize.lp:1030`, is present and unguarded by anything that was relaxed.

What remains is that the fully relaxed program does not finish: an unbounded solve ran past ten
minutes without producing a model. Every one of these cases is also in the slow-unsat class
below — `maven ^zlib-ng` takes 48 s unrelaxed. The likeliest reading is that the solver *can*
express the error and cannot find it in the time available, which makes this the same problem
as item 3 rather than a missing rule. A diagnosis needs either a faster encoding or clingo
unsat cores via assumptions, not more instrumentation.

One structural bug found along the way, unrelated to these cases but real:
`impossible_dependencies_check` (`asp.py:2564`) tests membership in `self.pkgs`, which is the
closure of the *input specs* and so already contains everything the user wrote after `^`. The
check cannot reject `foo ^bar` on those grounds. Recomputing the closure from the root names
alone makes it well-formed, but does not help here, for the reason above.

Not every silent constraint is worth relaxing. Turning `concretize.lp:847` ("the virtual build
dependency must be on the correct duplicate") into an error removes three of the ten internal
errors, but two of them come back as confident nonsense: `maven ^zlib-ng` starts reporting

    1. 'icedtea' cannot use 'icedtea' as a build-time provider of the 'java' virtual
    3. Multiple providers are required for the same 'java' virtual: 'icedtea' and 'openjdk'

with no configuration requiring either provider, and never a word about `^zlib-ng`. With the
constraint relaxed, a model that violates it — even at weight 100000 — is cheaper than the real
explanation, which remains out of reach. A wrong answer stated confidently is worse than
"submit a bug report", so this one stays hard. The same trade decided the weight of the
`concretize.lp:1034` rule, which is above every other error for exactly this reason.

Dependency cycles are a separate exception: `concretize.lp:2220-2221` uses clingo's `#edge`
acyclicity extension, not a rule, so a cycle is infeasible with no atom to attach a message to.
That one needs Python-side detection.

**3. Slow unsatisfiable inputs (14 timeouts).** Two shapes: `all: require: %gcc@99`, and
`X ^libpng` where libpng is not a dependency. `alglib ^libpng` runs past 45 s while `shc ^gmp`
— the same mutation — finishes in 5 s. A performance bug, not a message bug, but it costs the
user the message entirely. The timeout also covers only the clingo solve, not grounding or the
causation pass.

**4. Precision, not just recall.** Nothing yet measures whether a message names something the
user did *not* touch, which is the "wrong input blamed" complaint the first report opened with.
Each mutator would declare taboo tokens alongside `expect`.

## Reproducing

```sh
# hand-written CLI cases, ~2 min
python3 fuzzing/harness.py

# fuzz, 4 workers, ~10 min each
spack python fuzzing/fuzz.py --seed 101 --n 110 --max-deps 5 --out fuzz_1.jsonl
python3 fuzzing/analyze.py fuzz_*.jsonl

# A/B an existing corpus against a change: the records carry everything --replay needs
spack python fuzzing/fuzz.py --replay results/fuzz/fuzz_1.jsonl --out after_1.jsonl
```

`results/fuzz/fuzz_*.jsonl` + `analysis.txt` are the `develop` baseline;
`results/fuzz/after_*.jsonl` + `analysis_after.txt` are the same 440 inputs with all six
commits applied. Compare like with like: `--full` changes the message text, so a run with the
causation pass on is not comparable to one without.
