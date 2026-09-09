from spack.package import *

class Versiondep(Package):
    """Older versions need older leaf; newer need newer."""
    homepage = "https://example.com"
    has_code = False
    version("2.0")
    version("1.0")
    depends_on("leaf@1", when="@1")
    depends_on("leaf@2:", when="@2")
    def install(self, spec, prefix):
        pass
