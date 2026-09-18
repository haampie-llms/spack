# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import grp
import os
import pathlib
import stat
import time
import types

import pytest

import spack.config
import spack.hooks.permissions_setters
import spack.util.filesystem as fs
from spack.util.file_permissions import (
    InvalidPermissionsError,
    set_permissions,
    set_permissions_recursive,
)

pytestmark = pytest.mark.not_on_windows("chmod unsupported on Windows")


def ensure_known_group(path):
    """Ensure that the group of a file is one that's actually in our group list.

    On systems with remote groups, the primary user group may be remote and may not
    exist on the local system (i.e., it might just be a number). Trying to use chmod to
    setgid can fail silently in situations like this.
    """
    uid = os.getuid()
    gid = fs.group_ids(uid)[0]
    os.chown(path, uid, gid)


def test_chmod_real_entries_ignores_suid_sgid(tmp_path: pathlib.Path):
    path = tmp_path / "file"
    path.touch()
    mode = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
    os.chmod(str(path), mode)
    mode = os.stat(str(path)).st_mode  # adds a high bit we aren't concerned with

    perms = stat.S_IRWXU
    set_permissions(str(path), perms)

    assert os.stat(str(path)).st_mode == mode | perms & ~stat.S_IXUSR


def test_chmod_rejects_group_writable_suid(tmp_path: pathlib.Path):
    path = tmp_path / "file"
    path.touch()
    mode = stat.S_ISUID
    fs.chmod_x(str(path), mode)

    perms = stat.S_IWGRP
    with pytest.raises(InvalidPermissionsError):
        set_permissions(str(path), perms)


def test_chmod_rejects_world_writable_suid(tmp_path: pathlib.Path):
    path = tmp_path / "file"
    path.touch()
    mode = stat.S_ISUID
    fs.chmod_x(str(path), mode)

    perms = stat.S_IWOTH
    with pytest.raises(InvalidPermissionsError):
        set_permissions(str(path), perms)


def test_chmod_rejects_world_writable_sgid(tmp_path: pathlib.Path):
    path = tmp_path / "file"
    path.touch()
    ensure_known_group(str(path))

    mode = stat.S_ISGID
    fs.chmod_x(str(path), mode)

    perms = stat.S_IWOTH
    with pytest.raises(InvalidPermissionsError):
        set_permissions(str(path), perms)


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(str(path)).st_mode)


@pytest.fixture()
def tree(tmp_path: pathlib.Path):
    """A prefix with an executable, a data file, a symlink to a file and to a directory
    outside the prefix, and a subdirectory its owner cannot read."""
    prefix = tmp_path / "prefix"
    outside = tmp_path / "outside"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "share").mkdir()
    (prefix / "hidden").mkdir()
    (outside / "dir").mkdir(parents=True)

    exe = prefix / "bin" / "exe"
    exe.write_text("#!/bin/sh\n")
    os.chmod(str(exe), 0o700)
    data = prefix / "share" / "data"
    data.write_text("x")
    os.chmod(str(data), 0o600)
    child = prefix / "hidden" / "child"
    child.write_text("x")
    os.chmod(str(child), 0o600)
    os.chmod(str(prefix / "hidden"), 0o000)

    target_file = outside / "file"
    target_file.write_text("x")
    os.chmod(str(target_file), 0o600)
    os.chmod(str(outside / "dir"), 0o700)
    os.symlink(str(target_file), str(prefix / "link_file"))
    os.symlink(str(outside / "dir"), str(prefix / "link_dir"))
    yield prefix, outside
    # make sure pytest can clean up if a test fails before the hidden dir is fixed
    os.chmod(str(prefix / "hidden"), 0o700)


def test_set_permissions_recursive(tree):
    prefix, outside = tree
    gid = fs.group_ids(os.getuid())[0]
    file_perms = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
    dir_perms = file_perms | stat.S_ISGID

    set_permissions_recursive(str(prefix), file_perms, dir_perms, gid)

    for d in (prefix, prefix / "bin", prefix / "share", prefix / "hidden"):
        assert _mode(d) == dir_perms
        assert os.stat(str(d)).st_gid == gid
    # executable bits are only kept where the file already had one
    assert _mode(prefix / "bin" / "exe") == 0o750
    assert _mode(prefix / "share" / "data") == 0o640
    # the unreadable directory was fixed and descended into
    assert _mode(prefix / "hidden" / "child") == 0o640
    assert os.stat(str(prefix / "hidden" / "child")).st_gid == gid
    # symlink targets outside the prefix are untouched
    assert _mode(outside / "file") == 0o600
    assert _mode(outside / "dir") == 0o700


def test_set_permissions_recursive_is_a_noop_when_already_set(tree):
    prefix, _ = tree
    file_perms = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
    set_permissions_recursive(str(prefix), file_perms, file_perms, fs.group_ids(os.getuid())[0])

    paths = [str(prefix)] + [
        os.path.join(root, name)
        for root, dirs, files in os.walk(str(prefix))
        for name in dirs + files
    ]
    ctimes = [os.lstat(p).st_ctime_ns for p in paths]
    time.sleep(0.05)  # so that any chmod/chown would be visible as a newer ctime

    set_permissions_recursive(str(prefix), file_perms, file_perms, fs.group_ids(os.getuid())[0])

    assert [os.lstat(p).st_ctime_ns for p in paths] == ctimes


def test_permissions_hook(tree, mutable_config):
    prefix, _ = tree
    gid = fs.group_ids(os.getuid())[0]
    spack.config.CONFIG.set(
        "packages:all:permissions",
        {"read": "group", "write": "group", "group": grp.getgrgid(gid).gr_name},
    )
    spec = types.SimpleNamespace(prefix=str(prefix), external=False, name="pkg", concrete=False)

    spack.hooks.permissions_setters.post_install(spec)

    assert _mode(prefix) == 0o2770
    assert _mode(prefix / "hidden") == 0o2770
    assert _mode(prefix / "bin" / "exe") == 0o770
    assert _mode(prefix / "share" / "data") == 0o660
    assert os.stat(str(prefix / "share" / "data")).st_gid == gid


def test_set_permissions_skips_symlinks(tmp_path: pathlib.Path):
    target = tmp_path / "target"
    target.write_text("x")
    os.chmod(str(target), 0o600)
    link = tmp_path / "link"
    os.symlink(str(target), str(link))

    set_permissions(str(link), stat.S_IRWXU | stat.S_IRWXG)

    assert _mode(target) == 0o600
