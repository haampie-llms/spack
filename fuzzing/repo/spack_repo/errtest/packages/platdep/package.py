from spack.package import *

class Platdep(Package):
    """Dependency only on linux."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("leaf", when="platform=linux")
    def install(self, spec, prefix):
        pass
