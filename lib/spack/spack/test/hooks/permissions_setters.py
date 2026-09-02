# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import pathlib
import stat
import types

import pytest

import spack.hooks.permissions_setters as permissions_setters
import spack.package_prefs

pytestmark = [
    pytest.mark.not_on_windows("fd-relative system calls are POSIX only"),
    pytest.mark.skipif(os.getuid() == 0, reason="root can read files with mode 0"),
]


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(os.lstat(str(path)).st_mode)


def test_post_install_does_not_follow_symlinks(tmp_path: pathlib.Path, monkeypatch):
    """Symlinks in the prefix, whether to files or directories outside of it, are left alone;
    everything else gets the configured mode, including files that cannot be opened."""
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "file").write_text("x")
    (outside / "file").chmod(0o600)
    (outside / "dir").chmod(0o700)

    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "exe").write_text("x")
    (prefix / "bin" / "exe").chmod(0o700)
    (prefix / "bin" / "data").write_text("x")
    (prefix / "bin" / "data").chmod(0o600)
    (prefix / "bin" / "unreadable").write_text("x")
    (prefix / "bin" / "unreadable").chmod(0)
    os.mkfifo(str(prefix / "fifo"), 0o600)
    os.symlink(str(outside / "file"), str(prefix / "bin" / "link_to_file"))
    os.symlink(str(outside / "dir"), str(prefix / "link_to_dir"))
    os.symlink("nowhere", str(prefix / "dangling"))
    prefix.chmod(0o700)
    (prefix / "bin").chmod(0o700)

    monkeypatch.setattr(spack.package_prefs, "get_package_permissions", lambda spec: 0o775)
    monkeypatch.setattr(spack.package_prefs, "get_package_dir_permissions", lambda spec: 0o775)
    monkeypatch.setattr(spack.package_prefs, "get_package_group", lambda spec: None)

    permissions_setters.post_install(types.SimpleNamespace(prefix=str(prefix), external=False))

    assert _mode(prefix) == _mode(prefix / "bin") == 0o775
    assert _mode(prefix / "bin" / "exe") == 0o775
    assert _mode(prefix / "bin" / "data") == 0o664  # +X: no execute bits on non-executables
    assert _mode(prefix / "bin" / "unreadable") == 0o664
    assert _mode(prefix / "fifo") == 0o775  # +X only strips execute bits from regular files
    assert _mode(outside / "file") == 0o600
    assert _mode(outside / "dir") == 0o700
    for link in ("bin/link_to_file", "link_to_dir", "dangling"):
        assert os.path.islink(str(prefix / link))
