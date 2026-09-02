# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import stat as st
from typing import Optional, Union

import spack.package_prefs as pp
import spack.util.filesystem as fs
from spack.error import SpackError


def set_permissions_by_spec(path: Union[str, int], spec, dir_fd: Optional[int] = None) -> None:
    """Set the permissions configured for ``spec`` on ``path``, which may also be an open file
    descriptor. See :func:`set_permissions` for the meaning of ``dir_fd``."""
    if dir_fd is not None:
        mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
    else:
        mode = os.stat(path).st_mode
    if st.S_ISDIR(mode):
        perms = pp.get_package_dir_permissions(spec)
    else:
        perms = pp.get_package_permissions(spec)
    group = pp.get_package_group(spec)

    set_permissions(path, perms, group, dir_fd=dir_fd)


def set_permissions(
    path: Union[str, int], perms: int, group: Optional[str] = None, dir_fd: Optional[int] = None
) -> None:
    """Set ``perms`` (with ``+X`` semantics for the executable bits) and optionally the group on
    ``path``.

    ``path`` may be an open file descriptor, in which case the open file is changed and nothing
    is resolved by path. When ``dir_fd`` is given, ``path`` is resolved relative to that open
    directory and symlinks in the last component are not followed: this is the fallback for
    files that cannot be opened."""
    if dir_fd is not None:
        st_mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
    else:
        st_mode = os.stat(path).st_mode

    # Preserve higher-order bits of file permissions
    perms |= st_mode & (st.S_ISUID | st.S_ISGID | st.S_ISVTX)

    # Do not let users create world/group writable suid binaries
    if perms & st.S_ISUID:
        if perms & st.S_IWOTH:
            raise InvalidPermissionsError("Attempting to set suid with world writable")
        if perms & st.S_IWGRP:
            raise InvalidPermissionsError("Attempting to set suid with group writable")
    # Or world writable sgid binaries
    if perms & st.S_ISGID:
        if perms & st.S_IWOTH:
            raise InvalidPermissionsError("Attempting to set sgid with world writable")

    if dir_fd is not None:
        assert isinstance(path, str)
        if st.S_ISLNK(st_mode):
            return
        if st.S_ISREG(st_mode) and not st_mode & (st.S_IXUSR | st.S_IXGRP | st.S_IXOTH):
            perms &= ~(st.S_IXUSR | st.S_IXGRP | st.S_IXOTH)
        fs.chmod_nofollow(path, perms, dir_fd=dir_fd)
        if group:
            gid = fs.group_gid(group)
            if os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_gid != gid:
                os.chown(path, -1, gid, dir_fd=dir_fd, follow_symlinks=False)
        return

    fs.chmod_x(path, perms)

    if group:
        fs.chgrp(path, group, follow_symlinks=False)


class InvalidPermissionsError(SpackError):
    """Error class for invalid permission setters"""
