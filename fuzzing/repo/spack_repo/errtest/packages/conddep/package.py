from spack.package import *

class Conddep(Package):
    """Dependency only when variant is on."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    variant("withleaf", default=False, description="use leaf")
    depends_on("leaf", when="+withleaf")
    def install(self, spec, prefix):
        pass
