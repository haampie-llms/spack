from spack.package import *

class Depvariant(Package):
    """Requires leaf without shared."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("leaf~shared")
    def install(self, spec, prefix):
        pass
