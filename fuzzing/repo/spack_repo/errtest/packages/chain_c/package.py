from spack.package import *

class ChainC(Package):
    """Diamond: needs leaf@2 but chain-a -> chain-b needs leaf@1."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("chain-a")
    depends_on("leaf@2")
    def install(self, spec, prefix):
        pass
