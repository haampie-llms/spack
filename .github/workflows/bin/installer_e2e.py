"""End-to-end checks of how the new installer detects build exit and exit codes."""

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SPACK = [sys.executable, os.path.join(ROOT, "bin", "spack")]

PACKAGE = '''\
import os
import time

from spack_repo.builtin.build_systems.generic import Package

from spack.package import *


class {cls}(Package):
    """Test package."""

    homepage = "https://example.com"
    has_code = False

    version("1.0")

    def install(self, spec, prefix):
        {body}
'''

PACKAGES = {
    "xok": 'mkdirp(prefix.bin); touch(join_path(prefix.bin, "xok"))',
    "xfail": 'time.sleep(5); raise InstallError("intentional failure")',
    "xvanish": "mkdirp(prefix.bin); os._exit(0)",
    "xslow": "mkdirp(prefix.bin); time.sleep(300)",
    "xext": 'raise InstallError("externals are not built")',
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def setup(tmp):
    repo = os.path.join(tmp, "repos", "spack_repo", "e2e")
    write(os.path.join(repo, "repo.yaml"), "repo:\n  namespace: e2e\n  api: v2.2\n")
    for name, body in PACKAGES.items():
        cls = name.capitalize()
        write(
            os.path.join(repo, "packages", name, "package.py"), PACKAGE.format(cls=cls, body=body)
        )
    q = lambda p: p.replace("\\", "/")  # noqa: E731
    cfg = os.path.join(tmp, "cfg")
    write(
        os.path.join(cfg, "config.yaml"),
        f"config:\n  installer: new\n  concurrent_packages: 2\n"
        f"  locks: {'false' if sys.platform == 'win32' else 'true'}\n"
        f"  install_tree:\n    root: '{q(os.path.join(tmp, 'store'))}'\n"
        f"  build_stage: ['{q(os.path.join(tmp, 'stage'))}']\n",
    )
    write(os.path.join(cfg, "repos.yaml"), f"repos:\n  e2e: '{q(repo)}'\n")
    os.makedirs(os.path.join(tmp, "ext"))
    write(
        os.path.join(cfg, "packages.yaml"),
        f"packages:\n  xext:\n    buildable: false\n    externals:\n"
        f"    - spec: xext@1.0\n      prefix: '{q(os.path.join(tmp, 'ext'))}'\n",
    )
    env = dict(os.environ)
    env["SPACK_USER_CONFIG_PATH"] = cfg
    env["SPACK_USER_CACHE_PATH"] = os.path.join(tmp, "cache")
    return env


def spack(env, *args, timeout=600):
    start = time.monotonic()
    try:
        p = subprocess.run(
            SPACK + list(args),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, time.monotonic() - start
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return None, out, time.monotonic() - start


def main():
    tmp = tempfile.mkdtemp(dir=os.environ.get("RUNNER_TEMP"))
    env = setup(tmp)
    results = []

    def check(name, ok, out, secs, rc):
        status = "PASS" if ok else "FAIL"
        results.append(ok)
        print(f"::group::{status} {name} (exit={rc}, {secs:.1f}s)", flush=True)
        print(out, flush=True)
        print("::endgroup::", flush=True)

    def installed(name):
        return spack(env, "find", name)[0] == 0

    # Concretize once up front, so the timings below are about installing.
    rc, out, secs = spack(env, "spec", "xok", "xfail", "xvanish", "xslow", "xext")
    check("concretize", rc == 0, out, secs, rc)

    rc, out, secs = spack(env, "install", "xok")
    check("successful build is installed", rc == 0 and installed("xok"), out, secs, rc)

    rc, out, secs = spack(env, "install", "--show-log-on-error", "xfail")
    ok = rc not in (0, None) and "intentional failure" in out and not installed("xfail")
    check("failing build is reported as failed", ok, out, secs, rc)

    rc, out, secs = spack(env, "install", "xvanish", timeout=120)
    ok = rc not in (0, None) and not installed("xvanish")
    check("build exiting without exit code is a failure", ok, out, secs, rc)

    rc, out, secs = spack(env, "install", "xext")
    check("external is registered", rc == 0 and installed("xext"), out, secs, rc)

    rc, out, secs = spack(env, "install", "--fail-fast", "xfail", "xslow", timeout=200)
    ok = rc not in (0, None) and secs < 150 and not installed("xslow")
    check("fail-fast terminates a running build", ok, out, secs, rc)

    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
