"""Usage: spack -C <scope> python check.py [FDPROBE_OUT]

Checks the build log of the installed fd-probe for stdio markers, and on POSIX checks that the
direct children of Executable inherited no fds other than stdio and the advertised jobserver."""
import gzip
import re
import sys

import spack.store

errors = []
(spec,) = spack.store.STORE.db.query("fd-probe")
with open(spec.package.install_log_path, "rb") as f:
    data = f.read()
log = (gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data).decode(errors="replace")
print(f"--- build log of {spec.short_spec}\n{log}---")
for marker in ("NONE-OUT", "NONE-ERR", "STDIN-OUT", "STDIN-ERR", "CMD-OUT"):
    if f"MARKER-{marker}" not in log:
        errors.append(f"MARKER-{marker} missing from build log")

if len(sys.argv) > 1:
    text = open(sys.argv[1]).read()
    print(f"--- fd probe output\n{text}---")
    for section in text.split("=== ")[1:]:
        header, *lines = section.splitlines()
        label = header.split()[0]
        if label not in ("executable", "sh-c"):
            continue  # make recipes: what make passes on is up to make
        m = re.search(r"--jobserver-(?:auth|fds)=(\d+),(\d+)", header)
        allowed = {int(m.group(1)), int(m.group(2))} if m else set()
        if spec.satisfies("+gmake") and not allowed:
            errors.append(f"{label}: expected a pipe jobserver in MAKEFLAGS: {header}")
        inherited = {
            int(re.match(r"\s*fd=(\d+)", line).group(1)) for line in lines if " inherit " in line
        } - {0, 1, 2}
        if inherited != allowed:
            errors.append(f"{label}: inherited fds {sorted(inherited)}, expected {sorted(allowed)}")

print("\n".join(errors) or "OK")
sys.exit(1 if errors else 0)
