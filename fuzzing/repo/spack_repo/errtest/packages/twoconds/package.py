from spack.package import *

class Twoconds(Package):
    """Two variants imply incompatible dependency versions."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    variant("x", default=False, description="x")
    variant("y", default=False, description="y")
    depends_on("leaf@1", when="+x")
    depends_on("leaf@2", when="+y")
    def install(self, spec, prefix):
        pass
