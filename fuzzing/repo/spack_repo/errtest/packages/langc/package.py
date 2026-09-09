from spack.package import *

class Langc(Package):
    """Only needs C."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("c", type="build")
    def install(self, spec, prefix):
        pass
