# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Set the mode and group of installed files according to ``packages.yaml``.

On POSIX every change is applied with ``fchmod``/``fchown`` on an open file descriptor,
and the mode to set is computed from an ``fstat`` of that same descriptor. Directories
are opened with ``O_DIRECTORY | O_NOFOLLOW`` and their entries are opened relative to
that descriptor, so a symlink swapped into the tree while the walk is running is never
followed. The only path-based operations are for entries that cannot be opened, and
those use ``AT_SYMLINK_NOFOLLOW`` where the platform supports it.
"""

import errno
import os
import stat
import sys
from typing import Optional, Tuple, Union

import spack.package_prefs as pp
import spack.util.filesystem as fs
from spack.error import SpackError

if sys.platform != "win32":
    import grp

    _O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    #: Never block on a fifo, never acquire a controlling terminal, never follow a symlink.
    _O_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY

_HIGH_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def spec_permissions(spec) -> Tuple[int, int, Optional[int]]:
    """Resolve the configured ``(file_perms, dir_perms, gid)`` for a spec once."""
    file_perms = pp.get_package_permissions(spec)
    dir_perms = pp.get_package_dir_permissions(spec)
    return file_perms, dir_perms, _resolve_gid(pp.get_package_group(spec))


def set_permissions_by_spec(path: str, spec) -> None:
    """Set the configured mode and group of a single file or directory of ``spec``."""
    file_perms, dir_perms, gid = spec_permissions(spec)
    _set_permissions(path, file_perms, dir_perms, gid)


def set_permissions(path: str, perms: int, group: Union[str, int, None] = None) -> None:
    """Set the mode of a single path, preserving suid/sgid/sticky bits and treating the
    executable bits like ``chmod +X`` does. Symlinks are left alone."""
    _set_permissions(path, perms, perms, _resolve_gid(group))


def set_permissions_recursive(
    root: str, file_perms: int, dir_perms: int, gid: Optional[int] = None
) -> None:
    """Set the mode and group of ``root`` and everything below it, without following
    symlinks (they are skipped, and their targets are never modified)."""
    if sys.platform == "win32":
        _set_permissions(root, file_perms, dir_perms, gid)
        for dirpath, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = os.path.join(dirpath, name)
                if not os.path.islink(path):
                    _set_permissions(path, file_perms, dir_perms, gid)
        return

    fd = os.open(root, _O_DIR)
    try:
        _walk(fd, file_perms, dir_perms, gid)
    finally:
        os.close(fd)


def _walk(dirfd: int, file_perms: int, dir_perms: int, gid: Optional[int]) -> None:
    # Fix the directory itself first: entries can only be opened once we have search
    # permission on it.
    _apply_fd(dirfd, os.fstat(dirfd), dir_perms, gid)

    for name in os.listdir(dirfd):
        try:
            st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            continue

        if stat.S_ISLNK(st.st_mode):
            continue

        if stat.S_ISDIR(st.st_mode):
            child = _open_dir(name, dirfd, st, dir_perms, gid)
            if child is None:
                continue
            try:
                _walk(child, file_perms, dir_perms, gid)
            finally:
                os.close(child)

        elif stat.S_ISREG(st.st_mode):
            if _is_set(st, file_perms, gid):
                # Nothing to change. Deciding *not* to act from a path-based stat is safe:
                # whatever the entry is swapped for afterwards, we never write to it.
                continue
            try:
                fd = os.open(name, _O_FILE, dir_fd=dirfd)
            except OSError as e:
                if e.errno == errno.EACCES:
                    # Not readable by its owner, so we cannot get a descriptor for it.
                    _chmod_nofollow(name, st, file_perms, gid, dir_fd=dirfd)
                    continue
                if e.errno in (errno.ELOOP, errno.ENOENT):
                    # Replaced by a symlink or removed since the stat above.
                    continue
                raise
            try:
                fst = os.fstat(fd)
                if stat.S_ISREG(fst.st_mode):
                    _apply_fd(fd, fst, file_perms, gid)
            finally:
                os.close(fd)

        else:
            # fifo, socket, device: never open these, opening may block or have side effects.
            _chmod_nofollow(name, st, file_perms, gid, dir_fd=dirfd)


def _open_dir(
    name: str, dirfd: int, st: os.stat_result, dir_perms: int, gid: Optional[int]
) -> Optional[int]:
    """Open a subdirectory relative to ``dirfd``. Returns ``None`` if it was replaced by
    something that is not a directory, or removed, since it was listed."""
    for attempt in range(2):
        try:
            return os.open(name, _O_DIR, dir_fd=dirfd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                return None
            if e.errno == errno.EACCES and attempt == 0:
                # A directory without read permission for its owner: give ourselves
                # access first, then open it.
                _chmod_nofollow(name, st, dir_perms, gid, dir_fd=dirfd)
                continue
            raise
    return None


def _set_permissions(path: str, file_perms: int, dir_perms: int, gid: Optional[int]) -> None:
    if sys.platform == "win32":
        st = os.stat(path)
        perms = dir_perms if stat.S_ISDIR(st.st_mode) else file_perms
        fs.chmod_x(path, _target_mode(st.st_mode, perms))
        if gid is not None:
            fs.chgrp(path, gid, follow_symlinks=False)
        return

    try:
        fd = os.open(path, _O_FILE)
    except OSError as e:
        if e.errno == errno.ELOOP:
            # ``path`` is a symlink: never modify the target.
            return
        if e.errno == errno.EACCES:
            st = os.lstat(path)
            if not stat.S_ISLNK(st.st_mode):
                perms = dir_perms if stat.S_ISDIR(st.st_mode) else file_perms
                _chmod_nofollow(path, st, perms, gid)
            return
        raise
    try:
        st = os.fstat(fd)
        _apply_fd(fd, st, dir_perms if stat.S_ISDIR(st.st_mode) else file_perms, gid)
    finally:
        os.close(fd)


def _apply_fd(fd: int, st: os.stat_result, perms: int, gid: Optional[int]) -> None:
    """Set group and mode on an open descriptor, based on its own ``fstat`` result."""
    mode = _target_mode(st.st_mode, perms)
    chowned = False
    if gid is not None and st.st_gid != gid:
        # Group first: on Linux, chown of a regular file clears suid/sgid bits, and the
        # chmod below restores them since ``mode`` was derived from the pre-chown stat.
        os.fchown(fd, -1, gid)
        chowned = True
    if stat.S_IMODE(st.st_mode) != mode or (chowned and mode & _HIGH_BITS):
        os.fchmod(fd, mode)


def _chmod_nofollow(
    path: str, st: os.stat_result, perms: int, gid: Optional[int], dir_fd: Optional[int] = None
) -> None:
    """Path-based fallback for entries that are not opened. ``st`` is the entry's own
    ``lstat`` result and must not describe a symlink."""
    mode = _target_mode(st.st_mode, perms)
    chowned = False
    if gid is not None and st.st_gid != gid:
        os.chown(path, -1, gid, dir_fd=dir_fd, follow_symlinks=False)
        chowned = True
    if stat.S_IMODE(st.st_mode) == mode and not (chowned and mode & _HIGH_BITS):
        return
    try:
        os.chmod(path, mode, dir_fd=dir_fd, follow_symlinks=False)
    except NotImplementedError:
        # Linux with glibc < 2.32 and no fchmodat2: AT_SYMLINK_NOFOLLOW is not supported.
        # Re-check that the entry is still not a symlink, then chmod through the path.
        # The window between these two calls cannot be closed on such systems.
        if stat.S_ISLNK(os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            return
        os.chmod(path, mode, dir_fd=dir_fd)


def _is_set(st: os.stat_result, perms: int, gid: Optional[int]) -> bool:
    """Whether an entry already has the requested mode and group."""
    if gid is not None and st.st_gid != gid:
        return False
    return stat.S_IMODE(st.st_mode) == _target_mode(st.st_mode, perms)


def _target_mode(st_mode: int, perms: int) -> int:
    """The mode to set on an entry with current mode ``st_mode``: keep its suid/sgid/sticky
    bits, and only grant executable bits to regular files that already have one."""
    perms |= st_mode & _HIGH_BITS

    # Do not let users create world/group writable suid binaries
    if perms & stat.S_ISUID:
        if perms & stat.S_IWOTH:
            raise InvalidPermissionsError("Attempting to set suid with world writable")
        if perms & stat.S_IWGRP:
            raise InvalidPermissionsError("Attempting to set suid with group writable")
    # Or world writable sgid binaries
    if perms & stat.S_ISGID:
        if perms & stat.S_IWOTH:
            raise InvalidPermissionsError("Attempting to set sgid with world writable")

    if stat.S_ISREG(st_mode) and not st_mode & _EXEC_BITS:
        perms &= ~_EXEC_BITS
    return perms


def _resolve_gid(group: Union[str, int, None]) -> Optional[int]:
    if group is None or group == "":
        return None
    if isinstance(group, str):
        if sys.platform == "win32":
            raise OSError("Setting the group of installed files is not supported on Windows")
        return grp.getgrnam(group).gr_gid
    return group


class InvalidPermissionsError(SpackError):
    """Error class for invalid permission setters"""
