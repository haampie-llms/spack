# Concretization error messages: where they point away from the cause

Findings from 61 hand-written cases and 440 fuzzed cases run against the `builtin`
package repository on macOS arm64 (Darwin 24.6, `os=sequoia target=m4`) with apple-clang 17
and a Fortran-only gcc 14.2 registered as externals. Spack was at commit `261e4fbe4b`
on a branch a few commits ahead of `develop` `b495c19f9e`; the differences do not touch
error reporting.

All error text quoted below is verbatim from the runs in `results/`.

## Summary

Spack reports concretization failures in two passes. The first pass minimizes weighted
`error(...)` atoms and prints them. The second pass runs `error_messages.lp` over the
model to build a "required because ..." causal tree. Both passes share a structural
problem: the solver is holistic, so when the cheapest way to satisfy the input is to
change something the user did not mention, the message describes that change rather than
the input that forced it.

Four failure patterns account for nearly every bad message:

1. **Wrong input blamed.** The message names something that is not the cause.
   Systematic for conditional variants, foreign os and platform, and virtuals marked
   non-buildable.
2. **"Please submit a bug report" for a user error.** Hard constraints in
   `concretize.lp` with no matching `error()` rule fall through to the generic internal
   error. 14 of 440 fuzzed cases (3%).
3. **Only one side of a conflict named.** Every version conflict says which constraint
   failed and who wanted it, never who wanted the version that won.
4. **Generic message, one per node.** Requirements from `packages.yaml` report
   "cannot satisfy a requirement for package 'X'" with no requirement text, no file, and one
   line per package in the DAG. Up to 87 numbered lines in the fuzzed set.

## Root cause of pattern 1 for variants

`concretize.lp` has an intended diagnostic at lines 1547 and 1553:

    error(100, "Cannot set variant '{0}' for package '{1}' because the variant condition
                cannot be satisfied for the given spec", Variant, Package)

but a hard constraint at line 1618 makes the model infeasible before that error can be
weighed:

    :- attr("variant_set", node(ID, Package), Variant, Value),
       not attr("variant_value", node(ID, Package), Variant, Value).

A user-set variant that does not exist on the requested version has no `variant_value`
choice (the choice rule requires `node_has_variant`), so every model keeping the requested
version is infeasible. The only feasible models change the version, at a cost of one
`error(10000, "Cannot satisfy '{0}@{1}'")`. That is what gets reported:

    $ spack spec hdf5@1.8.19+java
    ==> Error: failed to concretize `hdf5@1.8.19+java` for the following reasons:
           1. Cannot satisfy 'hdf5@1.8.19'
    ==> Error:      2. Cannot satisfy 'hdf5@1.8.19' (version 1.14.6 does not match)
            required because hdf5@1.8.19+java requested explicitly

The `java` variant is declared `when="@1.10:"`. The message never mentions java. The
same shape appears when the requirement comes from `packages.yaml`
(`hdf5: require: +java` with root `hdf5@1.8.19`) and in environments.

## Hand-written cases

Inputs and outputs are in `results/handwritten/`. The synthetic `errtest` repository in
`repo/` isolates one mechanism per package.

### Wrong input blamed

| Input | Spack says | Actual cause |
|---|---|---|
| `condvar@1.0+feat` | Cannot satisfy condvar@1.0 (version 2.0 does not match) | variant only exists when @2: |
| `hdf5@1.8.19+java` | Cannot satisfy hdf5@1.8.19 (version 1.14.6 does not match) | variant only when @1.10: |
| `hdf5: require: +java` with `hdf5@1.8.19` | same, no mention of java or config | config requirement on a conditional variant |
| `zlib-ng os=ubuntu22.04` | Cannot select a single "node_os" | requested os is not the host os |
| `zlib-ng platform=linux` | Cannot select a single "node_platform" | host is darwin |
| `mpi: buildable: false` with `hdf5+mpi` | Cannot build openmpi, buildable:false | the user configured the virtual, not openmpi |
| `needsfortran %fortran=apple-clang` | 'needsfortran %fortran=apple-clang' cannot depend on apple-clang | apple-clang provides no fortran |
| `hdf5@1.14 ^cmake@3.12` | fourth message: Cannot satisfy 'cmake@3.12:' and 'cmake@3.12' | false statement; the third message is the right one |

### Internal error for a user error

- `platdep ^leaf` where the dependency exists only when `platform=linux`.
- `cyc-a`, a dependency cycle.
- `hdf5+fortran %fortran=apple-clang`.
- `zlib-ng %c=gcc %c=apple-clang`, two providers for one language.

### Only one side named

- `chain-c` (diamond: chain-c needs leaf@2, chain-a -> chain-b needs leaf@1) reports
  "Cannot satisfy 'leaf@1' (version 2.0 does not match)" with the chain-b path, and never
  mentions chain-c's own `depends_on("leaf@2")`.
- `twoconds+x+y` reports only the +x path.
- `hdf5~mpi ^mpich` says mpich is not a dependency but not that `~mpi` is why.
- "Multiple providers are required for the same 'mpi' virtual" never names the providers
  or that one came from `packages.yaml`. Its cause analysis always reports nothing
  because the rule in `error_messages.lp` line 129 references `has_provider`, which is
  defined nowhere. Clingo prints "atom does not occur in any rule head" for it in 11 of
  the 36 runs that reached the second pass.

### Generic, fan-out

- `all: require: ^mpich` with `hdf5 ^openmpi`: 12 lines of "cannot satisfy a requirement
  for package 'X'." including zlib-ng and perl, which never depend on MPI.
- `buildable: false` with an external that fails a constraint never says which external
  was considered or which constraint it failed. For `chain-a` with external `leaf@2.0`
  the cause (chain-b needs leaf@1) is invisible.
- Target errors print four lines; the second is the right one.

### Good

`conflicts()` and `requires()` with a `msg`, `depends_on` variant conflicts, config
`require: ~mpi` versus `+mpi`, plain `buildable: false` with no externals, nonexistent
variant or version in a requirement (caught before the solver), unknown hashes,
deprecated versions, invalid variant values.

### Silent successes

`hdf5 ^mpi@99` concretizes with mpi-serial because that package provides `mpi` with no
version. A provider list naming a nonexistent package, and a variant preference that does
not exist on the chosen version, both pass without a warning. A conditional requirement
of `+mpi when @1.14` against `hdf5~mpi` is dodged by choosing hdf5@2.2.0.

## Fuzzed cases

`fuzz.py` picks a random real package, reads its directives, and injects one known-bad
thing. Twenty-three mutators; 440 cases; solver timeout 45 s; causation pass disabled
for speed and re-enabled on a replayed subset (`results/fuzz/replay_full.jsonl`).

| Outcome | Cases |
|---|---|
| Unsatisfiable with a message | 258 |
| Rejected before the solver | 75 |
| Concretized anyway | 93 |
| Internal error | 14 |
| Solver timeout at 45 s | 14 |

### Cause-mention rate by mutation

"Any" means at least one expected token appeared in the message. "All" means every one
did, which is the bar for acting on the message without guessing. Full table in
`results/fuzz/analysis.txt`.

| Mutation | Any | All | Typical first-pass message |
|---|---|---|---|
| foreign os | 0/18 | 0/18 | Cannot select a single "node_os" |
| foreign platform | 2/18 | 2/18 | Cannot select a single "node_platform" |
| config requires another version | 0/23 | 0/23 | Cannot satisfy 'py-funcy@=1.14' |
| external too old | 10/10 | 0/10 | Cannot build cmake, buildable:false and no externals satisfy |
| config requires another provider | 5/5 | 0/5 | Multiple providers are required for the same 'qmake' virtual |
| dependency version out of range | 6/6 | 0/6 | Cannot satisfy 'cmake@=3.0.2' |
| `all: require` | 46/54 | 16/54 | cannot satisfy a requirement for package 'zlib-ng' |
| `%c=gcc` with Fortran-only gcc | 16/18 | 16/18 | Only external, or concrete, compilers are allowed for the c language |
| conflicts directive | 15/15 | 15/15 | the directive's own message |
| `buildable: false` on a dependency | 26/26 | 26/26 | Cannot build X, buildable:false |

The "any" hits in the middle rows are incidental: the package name appears, the injected
thing does not.

### What the causation pass rescues

Replaying with the second pass enabled, config version requirements and out-of-range
dependency versions gained a correct third message naming the requirement or the
`depends_on` constraint. External too old, foreign os, provider requirement conflicts, two
providers, and the compiler case gained nothing and printed "No additional error causes
discovered".

### New internal-error shapes

- Two values of a multi-valued variant whose conditional dependencies are disjoint:
  `nccl-tests cuda_arch=35,90`, `sundials cuda_arch=11,86`. Both generated cases.
- A package with no compiler dependencies plus an unrelated `^dep`: `maven ^zlib-ng`,
  `apktool ^libpng`, `apple-libuuid ^libiconv`. Five cases. The same mutation on a package
  with compiler dependencies gets the proper "not a dependency" message.
- One config version requirement (`nccl` at `@=2.9.8-1` with `require: @=2.10.3-1`),
  unlike the other 22.

### Slow unsatisfiable inputs

All 14 timeouts had one of two shapes: `all: require: %gcc@99` in `packages.yaml`, or
`X ^libpng` where libpng is not a dependency of X. The libpng cases matter because the
same mutation with a different dependency finishes in 5 s (`shc ^gmp`) while `alglib
^libpng` and `perl-http-tiny ^libpng` run past 45 s. Median solve time across the set was
5.5 s for both satisfiable and unsatisfiable inputs, so these are 10x outliers on trivial
inputs.

`concretizer:timeout` (seconds) and `concretizer:error_on_timeout` bound this. The cap
covers only the clingo solve phase, not setup, grounding, or the second-pass causation
solve. The expiry message names the spec and nothing else. With `error_on_timeout: false`
Spack reports the best model found so far, which for an unsatisfiable input means the
error list of that partial model.

### Token cost

Error text is short: median 325 characters, largest 5.5k. The waste is structural. Every
message is printed twice, once before and once after the causation pass, framed by
"Analyzing the cause of the failure" and "No additional error causes discovered". The
five longest messages were all `all: require` cases at 58 to 87 numbered lines, most of
them unrelated conflicts triggered by the solver's fallback choices ("icu4c:
platform=darwin conflicts with build_system=msbuild"). A successful `spack spec hdf5` is
48 lines and 6.5k characters, roughly twenty times a typical error.

### Small wording traps

- `%clang@99` reports "No version exists that satisfies llvm@99", a name the user never
  typed.
- Target errors lead with "Cannot select a single node_target" before the correct line.
- An external Python package declared without an external python is rejected with a
  message about a missing external for python, which is correct but reads as a typo.

### Expected successes

An undeclared exact version like `@=99.99.99` concretizes 56 of 59 times because Spack
1.x allows unknown exact versions. `buildable: false` on an already-installed dependency
succeeds through reuse. A `^dep` that is transitively reachable succeeds. Not bugs, but
an agent expecting an error will be surprised.

## Suggested fixes, in order of payoff

1. Turn the hard constraint at `concretize.lp:1618` into a weighted error for user-set
   variants, or emit the conditional-variant error before the version error. Fixes every
   variant row in pattern 1 and removes the bogus first entry from four otherwise good
   cases.
2. Rewrite os, platform, and target messages to "requested os=X, host is Y". Zero
   mentions in 36 fuzzed cases today.
3. Give the pattern 2 hard constraints `error()` rules: platform-conditional
   dependencies, cycles, language providers, multiple compilers per language, disjoint
   conditional dependencies from a multi-valued variant, unrelated `^dep` on
   compiler-free roots.
4. Make the cause tree show both sides of a version or provider conflict.
5. Put the requirement spec and its config file and line into requirement messages in
   the first pass, and collapse the per-node fan-out for `all:` requirements. The blame
   data already exists in `spack config blame`.
6. Define `has_provider` in `error_messages.lp` or drop the rule, so provider conflicts
   get causes and the clingo warning disappears.
7. List the externals considered and the failing constraint in `buildable: false`
   messages. Ten of ten fuzzed cases hid the cause.
8. Print each message once, drop the chatter when stdout is not a tty, and offer the
   numbered messages plus cause trees as JSON.
9. Apply the timeout to the causation pass too, and make the expiry message say the input
   may be unsatisfiable.
10. Treat slow unsat as its own bug class; `alglib ^libpng` is a minimal reproducer.
