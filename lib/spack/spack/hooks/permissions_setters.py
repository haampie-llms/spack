# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import errno
import os
import stat

import spack.util.file_permissions as fp
import spack.util.filesystem as fs


def _set_file_permissions(dir_fd: int, name: str, spec) -> None:
    """Set the configured permissions on the non-directory entry ``name`` of the directory open
    at ``dir_fd``, without ever following a symlink."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        return

    if stat.S_ISREG(st.st_mode):
        try:
            fd = fs.open_nofollow(name, dir_fd=dir_fd)
        except OSError as e:
            if fs._is_nofollow_error(e) or e.errno == errno.ENOENT:
                return
            if e.errno not in (errno.EACCES, errno.EPERM):
                raise
        else:
            try:
                fp.set_permissions_by_spec(fd, spec)
            finally:
                os.close(fd)
            return

    # Unreadable regular files and special files: change them relative to the directory fd.
    fp.set_permissions_by_spec(name, spec, dir_fd=dir_fd)


def post_install(spec, explicit=None):
    if spec.external:
        return

    if not fs._HAVE_DIR_FD or not hasattr(os, "fwalk"):
        fp.set_permissions_by_spec(spec.prefix, spec)

        # os.walk explicitly set not to follow links
        for root, dirs, files in os.walk(spec.prefix, followlinks=False):
            for d in dirs:
                if not os.path.islink(os.path.join(root, d)):
                    fp.set_permissions_by_spec(os.path.join(root, d), spec)
            for f in files:
                if not os.path.islink(os.path.join(root, f)):
                    fp.set_permissions_by_spec(os.path.join(root, f), spec)
        return

    # Every directory, including the prefix itself, is yielded with its own open file descriptor
    # and changed through it. Symlinked directories are listed in ``dirs`` but never yielded as
    # a descriptor, so they are skipped; symlinks to files are skipped in _set_file_permissions.
    for _, _, files, dir_fd in os.fwalk(spec.prefix, follow_symlinks=False):
        fp.set_permissions_by_spec(dir_fd, spec)
        for name in files:
            _set_file_permissions(dir_fd, name, spec)
