from spack.package import *

class ChainB(Package):
    """Requires leaf@1."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("leaf@1")
    def install(self, spec, prefix):
        pass
