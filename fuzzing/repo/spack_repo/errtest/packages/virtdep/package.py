from spack.package import *

class Virtdep(Package):
    """Needs a recent MPI."""
    homepage = "https://example.com"
    has_code = False
    version("1.0")
    depends_on("mpi@4:")
    def install(self, spec, prefix):
        pass
