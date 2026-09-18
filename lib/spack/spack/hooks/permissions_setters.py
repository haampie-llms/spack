# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import spack.util.file_permissions as fp


def post_install(spec, explicit=None):
    if spec.external:
        return
    file_perms, dir_perms, gid = fp.spec_permissions(spec)
    fp.set_permissions_recursive(spec.prefix, file_perms, dir_perms, gid)
