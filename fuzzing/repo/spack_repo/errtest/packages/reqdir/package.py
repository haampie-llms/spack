from spack.package import *

class Reqdir(Package):
    """requires() directive with message."""
    homepage = "https://example.com"
    has_code = False
    version("2.0")
    version("1.0")
    variant("feat", default=False, description="feature")
    requires("@2:", when="+feat", msg="feat needs version 2 or newer")
    def install(self, spec, prefix):
        pass
