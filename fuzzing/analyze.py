"""Bucket fuzzer results by message template and score cause-mention rate per mutation kind."""

import collections
import glob
import json
import os
import re
import sys

files = sys.argv[1:] or sorted(
    glob.glob(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "fuzz", "fuzz_*.jsonl")
    )
)
recs = []
for f in files:
    with open(f) as fh:
        for line in fh:
            if line.strip():
                recs.append(json.loads(line))

# --- templates from concretize.lp + known python-side messages
# Read the solver program from the checkout this script lives in, so that evaluating a fix in a
# worktree buckets its new messages instead of silently dropping them into OTHER:.
SPACK_ROOT = os.environ.get(
    "SPACK_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
lp = os.path.join(SPACK_ROOT, "lib", "spack", "spack", "solver", "concretize.lp")
try:
    with open(lp, encoding="utf-8") as fh:
        lp_source = fh.read()
except OSError as e:
    sys.exit(f"cannot read {lp}: {e}\nset SPACK_ROOT to the checkout under test")

lp_templates = []
for m in re.finditer(r'error\(\d+, *"([^"]*)"', lp_source):
    t = m.group(1)
    if t not in lp_templates:
        lp_templates.append(t)


def template_regex(t):
    return re.compile("^" + re.sub(r"\\\{\d+\\\}", "(.+?)", re.escape(t)) + "$")


compiled = [(t, template_regex(t)) for t in lp_templates]
compiled.sort(key=lambda x: -len(x[0]))

generic = [
    (r"Cannot satisfy '.+' and '.+'", "Cannot satisfy '{0}' and '{1}'"),
    (
        r"Cannot satisfy '.+' \(version .+ does not match\)",
        "Cannot satisfy '{0}' (version {1} does not match)",
    ),
    (
        r"cannot satisfy a requirement for package '.+'\.",
        "cannot satisfy a requirement for package '{0}'.",
    ),
    (
        r"No version exists that satisfies these input specs.*",
        "No version exists that satisfies these input specs",
    ),
    (r".*can only be satisfied by deprecated versions.*", "DeprecatedVersionError"),
    (
        r".*is not a possible dependency of any root spec",
        "InvalidDependencyError: not a possible dependency",
    ),
    (r".*cannot depend on .*", "'{spec}' cannot depend on {x}"),
    (r"No such variant .*", "UnknownVariantError"),
    (r"invalid values for variant .*", "InvalidVariantValueError"),
    (r"Spack concretizer internal error.*", "INTERNAL ERROR"),
    (r".*is unsatisfiable", "INTERNAL ERROR"),
    (r"Spack is taking more than.*", "TIMEOUT"),
    (
        r"Version requirement .* cannot match any known version.*",
        "Version requirement cannot match any known version",
    ),
    (r"cannot emit requirements for the solver.*", "cannot emit requirements for the solver"),
    (
        r"Cannot select a single \".+\" for package \".+\"",
        'Cannot select a single "{attr}" for package "{pkg}"',
    ),
    # Replacements for the line above, from hs/fix/error-msgs-1. These are built in asp.py
    # (ErrorHandler.multiple_values_error / no_value_error), not in concretize.lp, so they
    # cannot be scraped and have to be listed here.
    (
        r"Conflicting .+ values are required for package '.+':.*",
        "Conflicting {attr} values are required for package '{pkg}'",
    ),
    (
        r"No .+ value could be selected for package '.+'",
        "No {attr} value could be selected for package '{pkg}'",
    ),
    # Legacy forms. Message text changes between the runs this tool exists to compare, so
    # keep superseded wordings classifiable instead of letting old results fall into OTHER:.
    (
        r"Multiple providers are required for the same '.+' virtual$",
        "Multiple providers are required for the same '{0}' virtual [legacy]",
    ),
]


def classify_line(line):
    line = line.strip()
    line = re.sub(r"^\s*\d+\.\s*", "", line)
    for t, rx in compiled:
        if rx.match(line):
            return t
    for rx, name in generic:
        if re.match(rx, line, re.S):
            return name
    return "OTHER: " + line[:60]


def message_lines(msg):
    out = []
    for line in msg.splitlines():
        s = line.strip()
        if (
            not s
            or s.startswith("failed to concretize")
            or s.startswith("required because")
            or s.startswith("Run with")
        ):
            continue
        if s.startswith("==>") or s.startswith("Analyzing") or s.startswith("#"):
            continue
        out.append(s)
    return out


by_kind = collections.defaultdict(list)
templates = collections.Counter()
template_examples = {}
template_mention = collections.defaultdict(lambda: [0, 0])
for r in recs:
    by_kind[r["kind"]].append(r)
    if r["outcome"] in ("unsat", "presolver", "internal", "timeout", "exception"):
        lines = message_lines(r["message"])
        seen = set()
        for ln in lines:
            t = classify_line(ln)
            if t in seen:
                continue
            seen.add(t)
            templates[t] += 1
            template_examples.setdefault(t, (r["kind"], r["specs"], r.get("config"), ln[:160]))
        # per-template accuracy: did the message overall mention the expected tokens?
        for t in seen:
            template_mention[t][1] += 1
            if r["mentioned"]:
                template_mention[t][0] += 1

print(f"cases: {len(recs)}  outcomes: {dict(collections.Counter(r['outcome'] for r in recs))}")
print()
print(
    "== per mutation kind: n, outcomes, cause-mentioned rate among error outcomes, median lines, max seconds"  # noqa: E501
)
for kind, rs in sorted(by_kind.items()):
    # a case whose package could not concretize before the mutation grades the message against a
    # cause that is not the cause; it says nothing about the message
    rs = [r for r in rs if not r.get("base_unsat")]
    if not rs:
        continue
    errs = [r for r in rs if r["outcome"] != "sat"]
    # "Please submit a bug report" quotes the input spec back, so substring matching scores it as
    # naming the cause. It explains nothing; count it as a miss or a fix that replaces it with a
    # real message looks like a regression.
    ment = sum(1 for r in errs if r["mentioned"] and r["outcome"] != "internal")
    full = sum(
        1
        for r in errs
        if r["outcome"] != "internal" and len(r["mentioned"]) == len([t for t in r["expect"] if t])
    )
    outs = dict(collections.Counter(r["outcome"] for r in rs))
    lines = sorted(len(message_lines(r["message"])) for r in errs) or [0]
    secs = max(r["seconds"] for r in rs)
    rate = f"{ment}/{len(errs)} any, {full}/{len(errs)} all" if errs else "-"
    print(
        f"{kind:34s} n={len(rs):3d} {str(outs):55s} mentioned={rate:18s} medlines={lines[len(lines) // 2]:2d} max={secs:5.1f}s"  # noqa: E501
    )

print()
print("== message templates seen (count, mention-rate of cases containing it, example)")
for t, n in templates.most_common():
    m, tot = template_mention[t]
    kind, specs, cfg, ex = template_examples[t]
    print(f"{n:4d}  {m:3d}/{tot:<3d}  {t}")
    print(f"           e.g. [{kind}] {' , '.join(specs)}  cfg={json.dumps(cfg) if cfg else '-'}")
    print(f"                -> {ex}")

print()
print("== cases with NO expected token mentioned (grouped by kind), up to 3 each")
for kind, rs in sorted(by_kind.items()):
    bad = [r for r in rs if r["outcome"] != "sat" and not r["mentioned"]]
    for r in bad[:3]:
        print(
            f"[{kind}] {' , '.join(r['specs'])} cfg={json.dumps(r.get('config')) if r.get('config') else '-'} expect={r['expect']}"  # noqa: E501
        )
        for ln in message_lines(r["message"])[:4]:
            print("     | " + ln[:170])

print()
print("== internal errors / exceptions / timeouts")
for r in recs:
    if r["outcome"] in ("internal", "exception", "timeout"):
        print(
            f"[{r['kind']}] {' , '.join(r['specs'])} cfg={json.dumps(r.get('config')) if r.get('config') else '-'} -> {r['outcome']}: {r['message'][:200]!r}"  # noqa: E501
        )

print()
print("== unexpected successes (mutation did not make it unsat)")
for kind, rs in sorted(by_kind.items()):
    sat = [r for r in rs if r["outcome"] == "sat"]
    if sat:
        print(
            f"{kind:34s} {len(sat)}/{len(rs)}  e.g. {' , '.join(sat[0]['specs'])} cfg={json.dumps(sat[0].get('config')) if sat[0].get('config') else '-'}"  # noqa: E501
        )
