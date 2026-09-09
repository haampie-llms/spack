from spack.package import *

class Condvalue(Package):
    """Variant value only valid on newer versions."""
    homepage = "https://example.com"
    has_code = False
    version("2.0")
    version("1.0")
    variant("mode", default="a", values=("a", "b", conditional("c", when="@2:")), description="mode")
    def install(self, spec, prefix):
        pass
