from spack.package import *

class CycB(Package):
    """Cycle b."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("cyc-a")
    def install(self, spec, prefix):
        pass
