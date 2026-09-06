from spack.package import *

class Condvar(Package):
    """Variant only exists on newer versions."""
    homepage = "https://example.com"
    has_code = False
    version("2.0")
    version("1.0")
    variant("feat", default=False, when="@2:", description="feature")
    def install(self, spec, prefix):
        pass
