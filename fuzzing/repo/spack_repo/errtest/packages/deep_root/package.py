from spack.package import *

class DeepRoot(Package):
    """Long chain: deep-root -> chain-a -> chain-b -> leaf@1, plus deep-mid -> leaf@3."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("chain-a")
    depends_on("deep-mid")
    def install(self, spec, prefix):
        pass
