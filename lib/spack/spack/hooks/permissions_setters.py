# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import errno
import os
import sys

import spack.util.file_permissions as fp

#: Open for reading without following a final symlink. O_NONBLOCK and O_NOCTTY make sure a FIFO
#: or character device in the prefix cannot block the open or become the controlling terminal.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
)

#: What open(2) with O_NOFOLLOW raises for a symlink: ELOOP on Linux and macOS, EMLINK on FreeBSD
_SYMLINK_ERRNOS = (errno.ELOOP, errno.EMLINK, getattr(errno, "EFTYPE", errno.ELOOP))


def _post_install_by_path(spec) -> None:
    fp.set_permissions_by_spec(spec.prefix, spec)
    for root, dirs, files in os.walk(spec.prefix, followlinks=False):
        for entry in dirs + files:
            path = os.path.join(root, entry)
            if not os.path.islink(path):
                fp.set_permissions_by_spec(path, spec)


def post_install(spec, explicit=None):
    if spec.external:
        return

    if sys.platform == "win32":
        _post_install_by_path(spec)
        return

    # Every directory is visited through its own file descriptor, and every file is opened
    # relative to that descriptor without following symlinks. Modes and group are then changed
    # through the descriptor, so a symlink that shows up while the walk is in progress can never
    # redirect a chmod or chown outside of the prefix.
    for root, _, files, dir_fd in os.fwalk(spec.prefix, follow_symlinks=False):
        fp.set_permissions_by_spec(dir_fd, spec)
        for name in files:
            try:
                fd = os.open(name, _OPEN_FLAGS, dir_fd=dir_fd)
            except OSError as e:
                if e.errno in _SYMLINK_ERRNOS or e.errno == errno.ENOENT:
                    continue  # a symlink, or removed in the meantime
                if e.errno not in (errno.EACCES, errno.EPERM):
                    raise
                # Cannot be opened (e.g. mode 0): change it by path after checking it's no link.
                path = os.path.join(root, name)
                if not os.path.islink(path):
                    fp.set_permissions_by_spec(path, spec)
                continue
            try:
                fp.set_permissions_by_spec(fd, spec)
            finally:
                os.close(fd)
