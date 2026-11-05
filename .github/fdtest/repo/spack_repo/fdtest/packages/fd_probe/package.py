import os
import sys

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class FdProbe(Package):
    """Checks which stdio and fds child processes of a build get."""

    homepage = "http://www.example.com"
    has_code = False

    version("1.0")
    variant("gmake", default=False, description="Build with a (pipe jobserver) gmake dependency")
    depends_on("gmake", type="build", when="+gmake")

    def install(self, spec, prefix):
        mkdirp(prefix)
        touch(join_path(prefix, "fd-probe-done"))
        # stdio: children must write to the build log, whether or not stdio is redirected
        python = Executable(sys.executable)
        code = "import sys; print('MARKER-{0}-OUT'); print('MARKER-{0}-ERR', file=sys.stderr)"
        python("-c", code.format("NONE"))
        python("-c", code.format("STDIN"), input=os.devnull)
        out = python("-c", code.format("STR"), output=str, error=str)
        assert "MARKER-STR-OUT" in out and "MARKER-STR-ERR" in out, out
        if sys.platform == "win32":
            Executable("cmd")("/c", "echo MARKER-CMD-OUT")
            return
        Executable("/bin/sh")("-c", "echo MARKER-CMD-OUT")

        # fds: run the C probe directly, through make, and through sh
        probe = os.environ["FDPROBE"]
        Executable(probe)("executable")
        if spec.satisfies("+gmake"):
            # Only with a gmake dependency: system make < 4.4 rejects fifo style MAKEFLAGS
            with open("Makefile", "w") as f:
                f.write(f"all: a b\na:\n\t{probe} make-recipe-a\nb:\n\t{probe} make-recipe-b\n")
            Executable(spec["gmake"].prefix.bin.make)()
        Executable("/bin/sh")("-c", f"{probe} sh-c")
        # a daemon that outlives the build, like sccache or bazel
        if os.environ.get("FDPROBE_DAEMON"):
            Executable(probe)("daemon", os.environ["FDPROBE_DAEMON"])
