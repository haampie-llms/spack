from spack.package import *

class DeepMid(Package):
    """Needs leaf@3 via variant default."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    variant("newleaf", default=True, description="use leaf 3")
    depends_on("leaf@3", when="+newleaf")
    def install(self, spec, prefix):
        pass
