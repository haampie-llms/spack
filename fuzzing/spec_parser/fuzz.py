"""Grammar-based fuzzer for spack.spec_parser round trips.

Generates spec strings from the EBNF in spec_parser.py (minus the filename alternative), and
checks for each generated string s:

  1. Spec(s) parses (else: "REJECT", the grammar admits something the parser does not)
  2. Spec(str(Spec(s))) parses (else: "UNPARSEABLE", str() produced something the parser rejects)
  3. Spec(str(Spec(s))) == Spec(s) (else: "DIFFERENT", the round trip changed the spec)
  4. str(Spec(str(Spec(s)))) == str(Spec(s)) (else: "UNSTABLE", str() is not a fixed point)

Usage: python3 fuzzing/spec_parser/fuzz.py [iterations] [seed]

The buckets are printed largest first, with the four shortest cases of each. A bucket is a
failure kind plus the exception it raised, so a fix is judged by a bucket disappearing.
"""

import collections
import os.path
import random
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lib", "spack"))

import spack.spec  # noqa: E402
import spack.version  # noqa: E402

R = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000

ID_START = "abcdefghijklmnopqrstuvwxyz0123456789_"
ID_CHARS = ID_START + "-"
VID_CHARS = ID_CHARS + "."
REF_CHARS = VID_CHARS + "/"
BARE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_-+.,:=%^~/\\"
ANY_CHARS = BARE_CHARS + " []@\"'!#$&()<>?`{}|"
#: Keys with special meaning, which only take a string value and cannot be propagated
SPECIAL_KEYS = [
    "target", "namespace",
    "cflags", "cxxflags", "fflags", "ldflags", "ldlibs", "cppflags",
    "dev_path", "patches", "commit",
]
DEPTYPES = ["build", "link", "run", "test"]


def pick(seq):
    return R.choice(seq)


def chance(p):
    return R.random() < p


def ws(p=0.7):
    return " " if chance(p) else ""


def gen_id(n=None):
    n = n or R.randint(1, 4)
    return pick(ID_START) + "".join(pick(ID_CHARS) for _ in range(n - 1))


def gen_vid():
    n = R.randint(1, 5)
    v = pick(ID_START) + "".join(pick(VID_CHARS) for _ in range(n - 1))
    return v if v[-1] in ID_START else v + pick(ID_START)  # a version ends alphanumeric


def gen_ref():
    n = R.randint(1, 6)
    return pick(ID_START) + "".join(pick(REF_CHARS) for _ in range(n - 1))


def gen_namespace():
    return "".join(gen_id() + "." for _ in range(R.randint(1, 2)))


def gen_name():
    r = R.random()
    if r < 0.1:
        return "*"
    if r < 0.25:
        return gen_namespace() + gen_id()
    return gen_id()


def gen_version():
    return ("=" if chance(0.15) else "") + gen_vid()


def gen_version_range():
    lo = gen_vid() if chance(0.7) else ""
    hi = gen_vid() if chance(0.7) else ""
    return f"{lo}:{hi}"


def gen_git_version():
    if chance(0.5):
        return "".join(pick("0123456789abcdefABCDEF") for _ in range(40))
    return "git." + gen_ref()


def gen_version_item():
    """A version, an exact version, a range, or a git version with an optional constraint"""
    r = R.random()
    if r < 0.15:
        constraint = ""
        if chance(0.5):
            constraint = "=" + (gen_version_range() if chance(0.3) else gen_vid())
        return gen_git_version() + constraint
    if r < 0.5:
        return gen_version_range()
    return gen_version()


def gen_version_list():
    parts = [gen_version_item() for _ in range(R.randint(1, 3))]
    return (ws(0.3) + "," + ws(0.3)).join(parts)


def gen_bare_value():
    return "".join(pick(BARE_CHARS) for _ in range(R.randint(1, 6)))


def quote(inner, q=None):
    """No escaping: quote with the kind of quote that does not occur in the value"""
    q = q or pick("'\"")
    if q in inner:
        q = "'" if q == '"' else '"'
    if q in inner:  # both kinds of quotes cannot be written, drop one
        inner = inner.replace(q, "")
    return q + inner + q


def gen_quoted_value():
    inner = "".join(pick(ANY_CHARS) for _ in range(R.randint(0, 8)))
    return quote(inner)


def gen_value():
    return gen_quoted_value() if chance(0.3) else gen_bare_value()


def gen_variant():
    r = R.random()
    if chance(0.2):
        name = pick(SPECIAL_KEYS)
        value = gen_id() if name in ("namespace", "target") else gen_value()
        return name + pick(["=", ":="]) + value
    name = gen_id()
    while name == "when":
        name = gen_id()
    if r < 0.25:
        return pick("+~-") + ws(0.2) + name
    if r < 0.4:
        return pick(["++", "~~", "--"]) + ws(0.2) + name
    if r < 0.7:
        return name + pick(["=", ":="]) + gen_value()
    return name + pick(["==", ":=="]) + gen_value()


def gen_node_options():
    opts = []
    has_version = has_hash = False
    for _ in range(R.randint(0, 3)):
        r = R.random()
        if r < 0.3 and not has_version:
            has_version = True
            opts.append("@" + ws(0.2) + gen_version_list())
        elif r < 0.4 and not has_hash:
            has_hash = True
            opts.append("/" + "".join(pick(ID_START) for _ in range(R.randint(1, 8))))
        else:
            opts.append(gen_variant())
    return opts


def join_options(prefix, opts):
    """Join node options; a key=value pair or -variant needs whitespace after an id"""
    out = prefix
    for o in opts:
        # a bare value extends over any value character, so it must be followed by whitespace
        last_bare = "=" in out.rsplit(" ", 1)[-1] and out[-1] not in "'\""
        needs_ws = last_bare or (
            out and (out[-1] in ID_CHARS or out[-1] in VID_CHARS) and (
                o[0] not in "@/+~" or o.startswith("-")
            )
        )
        out += (" " if needs_ws else ws(0.5)) + o
    return out


def gen_node(depth):
    name = gen_name() if chance(0.8) else ""
    return join_options(name, gen_node_options())


def gen_when(depth):
    while True:
        w = gen_spec(depth + 1)
        if w.strip():
            return w


def gen_virtual_assignment():
    virtuals = ",".join(gen_id() for _ in range(R.randint(1, 3)))
    substitute = (gen_namespace() if chance(0.2) else "") + gen_id()
    return f"{virtuals}={substitute}"


def gen_dependency(depth):
    if chance(0.3):
        return join_options(gen_virtual_assignment(), gen_node_options())
    return gen_node(depth)


def gen_edge_properties(depth):
    attrs = []
    for _ in range(R.randint(0, 2)):
        r = R.random()
        if r < 0.4:
            attrs.append("virtuals=" + ",".join(gen_id() for _ in range(R.randint(1, 2))))
        elif r < 0.7:
            attrs.append("deptypes=" + ",".join(pick(DEPTYPES) for _ in range(R.randint(1, 2))))
        elif depth < 2:
            attrs.append("when=" + quote(gen_when(depth)))
    if depth < 2 and chance(0.5):
        attrs.append("when=" + gen_when(depth))
    return "[" + " ".join(attrs) + "]"


def gen_spec(depth=0):
    out = gen_node(depth)
    for _ in range(R.randint(0, 3 if depth == 0 else 1)):
        sigil = pick(["^", "%", "%%"])
        edge = gen_edge_properties(depth) if chance(0.3) else ""
        # a bare value swallows a directly following sigil: foo=bar%gcc is the value bar%gcc
        before = " " if "=" in out.rsplit(" ", 1)[-1] else ws(0.7)
        out += before + sigil + edge + ws(0.7) + gen_dependency(depth)
    return out


def signature(exc):
    first = str(exc).splitlines()[0] if str(exc) else ""
    # strip the offending text, keep the message
    return f"{type(exc).__name__}: {first[:60]}"


buckets = collections.defaultdict(list)
n_ok = 0
for i in range(N):
    s = gen_spec()
    try:
        a = spack.spec.Spec(s)
    except Exception as e:  # the grammar admits something the parser rejects
        buckets["REJECT " + signature(e)].append((s, None))
        continue
    t = str(a)
    try:
        b = spack.spec.Spec(t)
    except Exception as e:
        buckets["UNPARSEABLE " + signature(e)].append((s, t))
        continue
    try:
        different = a != b
    except spack.version.VersionLookupError:
        different = False  # comparing git versions looks up the package; rely on str() below
    except Exception as e:
        buckets["COMPARE-ERROR " + signature(e)].append((s, t))
        continue
    if different:
        buckets["DIFFERENT"].append((s, t, str(b)))
        continue
    u = str(b)
    if u != t:
        buckets["UNSTABLE"].append((s, t, u))
        continue
    n_ok += 1

print(f"{N} generated, {n_ok} round-trip cleanly, {N - n_ok} problems in {len(buckets)} buckets\n")
for key, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
    items.sort(key=lambda it: len(it[0]))
    print(f"== {key}  ({len(items)})")
    for it in items[:4]:
        print("   " + " | ".join(repr(x) for x in it))
    print()
