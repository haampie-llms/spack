# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack.package import *


class ManyConditionalDeps(Package):
    """Simple package with one optional dependency"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/a-1.0.tar.gz"

    version("1.0")

    variant("cuda", description="enable foo dependencies", default=True)
    variant("rocm", description="enable bar dependencies", default=True)
    variant("fortran", description="enable fortran bindings", default=False)

    depends_on("c", type="build")
    depends_on("fortran", type="build", when="+fortran")

    for i in range(30):
        depends_on(f"gpu-dep +cuda cuda_arch={i}", when=f"+cuda cuda_arch={i}")

    for i in range(30):
        depends_on(f"gpu-dep +rocm amdgpu_target={i}", when=f"+rocm amdgpu_target={i}")

    # unlike the forwarded values above, these cannot be collapsed into a single line
    for i in range(30):
        depends_on(f"gpu-dep@{i}", when=f"@1.0 +cuda cuda_arch={i}")

    # the same constraint under conditions that differ in one value only: merged into one line
    for i in range(30):
        depends_on("gpu-dep@:1", when=f"@1.0: +cuda cuda_arch={i}")

    # conflicts sharing a condition are merged into one line too
    conflicts("+cuda", when="@:0.9")
    conflicts("+rocm", when="@:0.9")
    conflicts("+cuda", when="+rocm", msg="pick one GPU backend")
