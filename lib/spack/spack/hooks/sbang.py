# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import re
import secrets
import shutil
import stat
import sys
from typing import Iterable, Optional

import spack.error
import spack.store
import spack.util.filesystem as fs
from spack.util import tty

#: OS-imposed character limit for shebang line: 127 for Linux; 511 for Mac.
#: Different Linux distributions have different limits, but 127 is the
#: smallest among all modern versions.
if sys.platform == "darwin":
    system_shebang_limit = 511
else:
    system_shebang_limit = 127
    try:
        # searching for line '#define BINPRM_BUF_SIZE 256' in /usr/include/linux/binfmts.h
        # the nbr-1 is the sbang limit on the linux platform
        sbang_limit_re = re.compile("#define BINPRM_BUF_SIZE ([0-9]+)")
        with open("/usr/include/linux/binfmts.h", "r", encoding="utf-8") as f:
            for line in f:
                m = sbang_limit_re.match(line)
                if m:
                    system_shebang_limit = int(m.group(1)) - 1
    except Exception:
        # ignore any error a sane default is set already
        pass

#: Spack itself also limits the shebang line to at most 4KB, which should be plenty.
spack_shebang_limit = 4096

interpreter_regex = re.compile(b"#![ \t]*?([^ \t\0\n]+)")


def sbang_install_path():
    """Location sbang is installed within the install tree."""
    sbang_root = str(spack.store.STORE.unpadded_root)
    install_path = os.path.join(sbang_root, "bin", "sbang")
    path_length = len(install_path)
    if path_length > system_shebang_limit:
        msg = (
            "Install tree root is too long. Spack cannot patch shebang lines"
            " when script path length ({0}) exceeds limit ({1}).\n  {2}"
        )
        msg = msg.format(path_length, system_shebang_limit, install_path)
        raise SbangPathError(msg)
    return install_path


def sbang_shebang_line():
    """Full shebang line that should be prepended to files to use sbang.

    The line returned does not have a final newline (caller should add it
    if needed).

    This should be the only place in Spack that knows about what
    interpreter we use for ``sbang``.
    """
    return "#!/bin/sh %s" % sbang_install_path()


def get_interpreter(binary_string):
    # The interpreter may be preceded with ' ' and \t, is itself any byte that
    # follows until the first occurrence of ' ', \t, \0, \n or end of file.
    match = interpreter_regex.match(binary_string)
    return None if match is None else match.group(1)


def _filter_shebang_at(dir_fd: Optional[int], name: str, path: str) -> bool:
    """Implementation of :func:`filter_shebang` relative to an open directory file descriptor.

    Args:
        dir_fd: open file descriptor of the directory containing the file, or ``None`` to resolve
            ``name`` by path
        name: name of the file relative to ``dir_fd``, or its path if ``dir_fd`` is ``None``
        path: full path of the file, used for messages

    The file is opened with ``O_NOFOLLOW`` and its patched version is written to a temporary
    file in the same directory, which then replaces it with ``rename(2)`` relative to ``dir_fd``,
    so a symlink that appears in the prefix while the hook runs is never followed. Replacing the
    file requires write permission on the directory, not on the file, so read-only files are
    handled without changing their mode; the patched file has the same mode as the original."""
    try:
        fd = fs.open_nofollow(name, dir_fd=dir_fd)
    except OSError as e:
        if fs._is_nofollow_error(e):
            return False
        raise

    with os.fdopen(fd, "rb") as original:
        # If there is no shebang, we shouldn't replace anything.
        old_shebang_line = original.read(2)
        if old_shebang_line != b"#!":
            return False

        # Stop reading after b'\n'. Note that old_shebang_line includes the first b'\n'.
        old_shebang_line += original.readline(spack_shebang_limit - 2)

        # If the shebang line is short, we don't have to do anything.
        if len(old_shebang_line) <= system_shebang_limit:
            return False

        # Whenever we can't find a newline within the maximum number of bytes, we will
        # not attempt to rewrite it. In principle we could still get the interpreter if
        # only the arguments are truncated, but note that for PHP we need the full line
        # since we have to append `?>` to it. Since our shebang limit is already very
        # generous, it's unlikely to happen, and it should be fine to ignore.
        if len(old_shebang_line) == spack_shebang_limit and old_shebang_line[-1] != b"\n":
            return False

        # This line will be prepended to file
        new_sbang_line = (sbang_shebang_line() + "\n").encode("utf-8")

        # Skip files that are already using sbang.
        if old_shebang_line == new_sbang_line:
            return False

        interpreter = get_interpreter(old_shebang_line)

        # If there was only whitespace we don't have to do anything.
        if not interpreter:
            return False

        # Store the file permissions, the patched version needs the same.
        saved_mode = stat.S_IMODE(os.fstat(original.fileno()).st_mode)

        # Write the patched file next to the original, then atomically replace the original.
        dirname, basename = os.path.split(name)
        tmp_name = os.path.join(dirname, f".{basename}.sbang-{secrets.token_hex(4)}")
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | fs._NOFOLLOW_FLAGS,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(tmp_fd, "wb") as patched:
                patched.write(new_sbang_line)

                # Note that in Python this does not go out of bounds even if interpreter is a
                # short byte array.
                # Note: if the interpreter string was encoded with UTF-16, there would have
                # been a \0 byte between all characters of lua, node, php; meaning that it would
                # lead to truncation of the interpreter. So we don't have to worry about weird
                # encodings here, and just looking at bytes is justified.
                if interpreter[-4:] == b"/lua" or interpreter[-7:] == b"/luajit":
                    # Use --! instead of #! on second line for lua.
                    patched.write(b"--!" + old_shebang_line[2:])
                elif interpreter[-5:] == b"/node":
                    # Use //! instead of #! on second line for node.js.
                    patched.write(b"//!" + old_shebang_line[2:])
                elif interpreter[-4:] == b"/php":
                    # Use <?php #!... ?> instead of #!... on second line for php.
                    patched.write(b"<?php " + old_shebang_line + b" ?>")
                else:
                    patched.write(old_shebang_line)

                # Copy the remainder of the file, and give the patched file the original's mode.
                shutil.copyfileobj(original, patched)
                os.fchmod(patched.fileno(), saved_mode)

            os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass
            raise

    return True


def filter_shebang(path: str) -> bool:
    """
    Adds a second shebang line, using sbang, at the beginning of a file, if necessary.
    Note: Spack imposes a relaxed shebang line limit, meaning that a newline or end of
    file must occur before ``spack_shebang_limit`` bytes. If not, the file is not
    patched.
    """
    with fs.directory_fd(os.path.dirname(path) or ".") as dir_fd:
        name = os.path.basename(path) if dir_fd is not None else path
        return _filter_shebang_at(dir_fd, name, path)


def _filter_shebangs_at(dir_fd: Optional[int], directory: str, filenames: Iterable[str]) -> None:
    """Filter the shebangs of the executable, non-symlink regular files ``filenames`` in the
    directory open at ``dir_fd`` (or at ``directory`` if ``dir_fd`` is ``None``)."""
    is_exe = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

    for file in filenames:
        path = os.path.join(directory, file)
        name = file if dir_fd is not None else path

        # Only look at executable, non-symlink files.
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue

        if not stat.S_ISREG(st.st_mode) or not st.st_mode & is_exe:
            continue

        # test the file for a long shebang, and filter
        if _filter_shebang_at(dir_fd, name, path):
            tty.debug("Patched overlong shebang in %s" % path)


def filter_shebangs_in_directory(directory: str, filenames: Optional[Iterable[str]] = None):
    with fs.directory_fd(directory) as dir_fd:
        if filenames is None:
            filenames = os.listdir(directory if dir_fd is None else dir_fd)
        _filter_shebangs_at(dir_fd, directory, filenames)


def post_install(spec, explicit=None):
    """This hook edits scripts so that they call /bin/bash
    $spack_prefix/bin/sbang instead of something longer than the
    shebang limit.
    """
    if sys.platform == "win32":
        return
    if spec.external:
        tty.debug("SKIP: shebang filtering [external package]")
        return

    # Every directory is visited through its own file descriptor; symlinked directories are
    # never entered, and files are opened and replaced relative to that descriptor.
    for directory, _, filenames, dir_fd in os.fwalk(spec.prefix, follow_symlinks=False):
        _filter_shebangs_at(dir_fd, directory, filenames)


class SbangPathError(spack.error.SpackError):
    """Raised when the install tree root is too long for sbang to work."""
