from spack.package import *

class CycA(Package):
    """Cycle a."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("cyc-b")
    def install(self, spec, prefix):
        pass
