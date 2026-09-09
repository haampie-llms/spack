from spack.package import *

class ChainA(Package):
    """Depends on chain-b."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("chain-b")
    def install(self, spec, prefix):
        pass
