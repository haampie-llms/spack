from spack.package import *

class Needsfortran(Package):
    """Needs a Fortran compiler."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("fortran", type="build")
    def install(self, spec, prefix):
        pass
