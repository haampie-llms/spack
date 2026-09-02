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


@pytest.fixture
def prefix_with_escaping_links(tmp_path: pathlib.Path, monkeypatch):
    """An install prefix containing symlinks to files and directories outside of it."""
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "secret").write_text("x")
    (outside / "dir" / "secret").write_text("x")
    (outside / "secret").chmod(0o600)
    (outside / "dir" / "secret").chmod(0o600)
    (outside / "dir").chmod(0o700)

    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib").mkdir()
    (prefix / "bin" / "real").write_text("x")
    (prefix / "bin" / "real").chmod(0o600)
    (prefix / "bin" / "exe").write_text("x")
    (prefix / "bin" / "exe").chmod(0o700)
    (prefix / "bin" / "unreadable").write_text("x")
    (prefix / "bin" / "unreadable").chmod(0)
    os.mkfifo(str(prefix / "lib" / "fifo"), 0o600)
    os.symlink(str(outside / "secret"), str(prefix / "bin" / "link_f"))
    os.symlink(str(outside / "dir"), str(prefix / "lib" / "link_d"))
    os.symlink("nowhere", str(prefix / "lib" / "dangling"))
    (prefix / "lib").chmod(0o700)
    prefix.chmod(0o700)

    monkeypatch.setattr(spack.package_prefs, "get_package_permissions", lambda spec: 0o775)
    monkeypatch.setattr(spack.package_prefs, "get_package_dir_permissions", lambda spec: 0o775)
    monkeypatch.setattr(spack.package_prefs, "get_package_group", lambda spec: None)
    return prefix, outside


def test_post_install_sets_permissions_without_following_symlinks(prefix_with_escaping_links):
    prefix, outside = prefix_with_escaping_links
    spec = types.SimpleNamespace(prefix=str(prefix), external=False)

    permissions_setters.post_install(spec)

    assert _mode(prefix) == 0o775
    assert _mode(prefix / "bin") == 0o775
    assert _mode(prefix / "lib") == 0o775
    assert _mode(prefix / "bin" / "real") == 0o664  # +X strips x bits from non-executables
    assert _mode(prefix / "bin" / "exe") == 0o775
    assert _mode(prefix / "bin" / "unreadable") == 0o664
    assert _mode(prefix / "lib" / "fifo") == 0o775  # +X only applies to regular files

    # nothing outside of the prefix was touched
    assert _mode(outside / "secret") == 0o600
    assert _mode(outside / "dir") == 0o700
    assert _mode(outside / "dir" / "secret") == 0o600
    assert os.path.islink(str(prefix / "bin" / "link_f"))
    assert os.path.islink(str(prefix / "lib" / "link_d"))
    assert os.path.islink(str(prefix / "lib" / "dangling"))


def test_post_install_skips_externals(prefix_with_escaping_links):
    prefix, _ = prefix_with_escaping_links
    spec = types.SimpleNamespace(prefix=str(prefix), external=True)
    permissions_setters.post_install(spec)
    assert _mode(prefix) == 0o700
    assert _mode(prefix / "bin" / "real") == 0o600
