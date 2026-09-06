# Concretization error-message fuzzing

Tools for generating a wide variety of unsatisfiable concretization inputs and grading
whether Spack's error message points at the actual cause. Findings are in
[REPORT.md](REPORT.md).

Nothing here is wired into the test suite or CI. Everything runs against the
`spack` in the parent checkout (override with `SPACK=/path/to/bin/spack`).

## Contents

| Path | What it is |
|---|---|
| `fuzz.py` | Mutation-based fuzzer. Picks real packages, injects one known-bad thing, records the tokens an accurate message would have to mention, runs the solver in-process. |
| `analyze.py` | Buckets fuzzer output by message template and scores cause-mention rate per mutation kind. |
| `harness.py` | Hand-written cases: root specs, `packages.yaml` scopes, and environments, run through the CLI so the output is exactly what a user sees. |
| `repo/` | Synthetic package repository `errtest` with packages that isolate one failure mechanism each (conditional variant, diamond version conflict, cycle, platform-conditional dependency, ...). Used by `harness.py`. |
| `results/handwritten/` | One text file per hand-written case with the full CLI output, plus `summary.json`. |
| `results/fuzz/` | Raw fuzzer output (`fuzz_*.jsonl`), the replayed subset with the causation pass enabled (`replay_full.jsonl`), and `analysis.txt` from `analyze.py`. |

## Running

Hand-written cases (about 5 minutes, writes `results/handwritten/`):

```
python3 fuzzing/harness.py            # all cases
python3 fuzzing/harness.py R2 C0      # cases whose name starts with R2 or C0
```

Fuzzer (each worker takes roughly 10 minutes for 100 cases):

```
spack python fuzzing/fuzz.py --seed 101 --n 100 --max-deps 5 --out fuzz_1.jsonl
spack python fuzzing/fuzz.py --seed 102 --n 100 --max-deps 5 --out fuzz_2.jsonl
python3 fuzzing/analyze.py fuzz_1.jsonl fuzz_2.jsonl
```

By default the fuzzer patches out the second-pass causation solve so a case costs one
solve. To see the full user-facing message for a subset, replay it with `--full`:

```
spack python fuzzing/fuzz.py --replay cases.jsonl --full --out cases_full.jsonl
```

Useful flags: `--kinds cond_variant,cfg_all_require` restricts mutators, `--timeout N`
sets `concretizer:timeout` for the run (default 45 s), `--max-deps N` skips packages with
more direct dependencies most of the time to keep solves fast.

## Fuzzer record format

One JSON object per line:

```
kind        mutation name, e.g. "cond_variant"
package     package the mutation started from
specs       root spec(s) handed to the solver (two or more means solved together)
config      extra configuration pushed as a scope, or null
expect      tokens an accurate message would mention
outcome     sat | unsat | presolver | internal | timeout | exception
message     the error text
mentioned   subset of `expect` found in `message`
seconds     wall time of the solve
```

## Adding a mutator

A mutator is a function taking a package class and returning either `None` (not
applicable) or a dict with `kind`, `specs`, `config`, and `expect`. Add it to `MUTATORS`
in `fuzz.py`. The package class exposes `versions`, `variant_items()`, `dependencies`,
`conflicts`, `requirements`, and `provided`, which is enough to construct a spec that
violates a specific directive.
