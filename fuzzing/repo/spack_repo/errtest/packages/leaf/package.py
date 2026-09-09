from spack.package import *

class Leaf(Package):
    """Leaf package."""
    homepage = "https://example.com"
    has_code = False
    version("3.0")
    version("2.0")
    version("1.0")
    variant("shared", default=True, description="shared")
    def install(self, spec, prefix):
        pass
