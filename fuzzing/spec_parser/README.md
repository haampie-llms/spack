# Spec parser round-trip fuzzing

`fuzz.py` generates spec strings from the grammar in `lib/spack/spack/spec_parser.py` (minus the
filename alternative) and checks that a spec survives being printed and read back:

| bucket | what it means |
|---|---|
| `REJECT` | the grammar produced a string the parser does not accept |
| `UNPARSEABLE` | `str(spec)` produced a string the parser cannot read back |
| `DIFFERENT` | the string reads back as a spec that is not equal to the original |
| `UNSTABLE` | printing the re-parsed spec gives a different string, so `str()` is not a fixed point |

Buckets are keyed by kind plus the exception, printed largest first with the four shortest cases
of each, so a fix shows up as a bucket disappearing. Generation only depends on the seed, so two
branches see the same strings.

```console
$ python3 fuzzing/spec_parser/fuzz.py [iterations] [seed]     # from the repo root
```

Most `REJECT` buckets are noise: the grammar is syntax only, so it happily writes `@6u:5` (an
empty range) or a `when=` condition naming two packages. The buckets worth reading are the other
three, and any `REJECT` whose error is not about the meaning of the string.

This is what the round-trip fixes on this branch were found with. On `develop`, 20000 strings at
seed 1 give 595 `UNPARSEABLE` and 105 `DIFFERENT`; here `UNPARSEABLE` and `UNSTABLE` are empty,
and the cases that shrank to something readable are regression tests in
`lib/spack/spack/test/spec_syntax.py`. The 72 `DIFFERENT` that remain are not the parser's: a
40-hex version with uppercase digits, which the tokenizer reads as a git hash but
`is_git_commit_sha()` does not (45); a compiler flag given twice, which prints as one value (26);
and the version `_` (1).

Nothing here runs in CI.
