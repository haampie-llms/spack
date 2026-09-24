# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import os
import pathlib

import pytest

import spack.cmd.isolate
import spack.config
import spack.test.harness
from spack.context import SpackContext

sp_isolate = spack.test.harness.SpackCommand("isolate")
sp_config = spack.test.harness.SpackCommand("config")


def _reread(cfg_dir: pathlib.Path) -> SpackContext:
    """A context reading the configuration in ``cfg_dir`` again, after isolation changed it."""
    return SpackContext(spack.config.create_from(str(cfg_dir / "spack")))


@pytest.fixture(scope="function")
def mock_pre_isolate_config(mutable_config, monkeypatch, tmp_path):
    # The spack scope of the mutable configuration is a copy of the mock configuration
    cfg_dir = pathlib.Path(mutable_config.scopes["spack"].path).parent
    include_path = cfg_dir / "spack" / "include.yaml"
    isolate_path = cfg_dir / "isolate"
    preserved_include_path = cfg_dir / "spack" / ".isolate.include.yaml"
    # These paths usually live in spack/etc/spack
    monkeypatch.setattr(spack.cmd.isolate, "INCLUDE_PATH", str(include_path))
    monkeypatch.setattr(spack.cmd.isolate, "ISOLATE_SCOPE_PATH", str(isolate_path))
    monkeypatch.setattr(spack.cmd.isolate, "PRESERVED_INCLUDE_PATH", str(preserved_include_path))

    yield cfg_dir, tmp_path


def test_isolate_smoke_test(mock_pre_isolate_config):
    cfg_dir, iso_root = mock_pre_isolate_config
    isolated_path = iso_root / "test-isolation"
    sp_isolate("--path", str(isolated_path))
    assert os.path.exists(spack.cmd.isolate.ISOLATE_SCOPE_PATH)
    assert os.path.exists(spack.cmd.isolate.PRESERVED_INCLUDE_PATH)
    assert isolated_path.exists()
    assert os.path.exists(os.path.join(spack.cmd.isolate.ISOLATE_SCOPE_PATH, "bootstrap.yaml"))
    assert os.path.exists(os.path.join(spack.cmd.isolate.ISOLATE_SCOPE_PATH, "config.yaml"))
    # we reload the config after isolation
    reread_ctx = _reread(cfg_dir)
    assert "isolate" in sp_config("scopes", ctx=reread_ctx)


def test_isolate_added_config(mock_pre_isolate_config):
    cfg_dir, iso_root = mock_pre_isolate_config
    isolated_path = iso_root / "test-isolation"
    sp_isolate("--path", str(isolated_path))
    # configuration has changed on disk, this refreshes it in memory
    reread_ctx = _reread(cfg_dir)
    sp_config("add", "config:build_jobs:42", ctx=reread_ctx)
    assert (isolated_path / "config.yaml").exists()
    with open(isolated_path / "config.yaml", "r", encoding="utf-8") as f:
        text = f.read().strip()
    expected_text = """\
config:
  build_jobs: 42"""
    assert text == expected_text


def test_isolate_overwrite_same_dir(mock_pre_isolate_config):
    _, iso_root = mock_pre_isolate_config
    isolated_path1 = iso_root / "test-isolation1"
    sp_isolate("--path", str(isolated_path1))
    with pytest.raises(Exception):
        sp_isolate("--path", str(isolated_path1))
    sp_isolate("--overwrite", "--path", str(isolated_path1))


def test_isolate_overwrite_different_dir(mock_pre_isolate_config):
    cfg_dir, iso_root = mock_pre_isolate_config
    isolated_path1 = iso_root / "test-isolation1"
    isolated_path2 = iso_root / "test-isolation2"
    sp_isolate("--path", str(isolated_path1))
    with pytest.raises(Exception):
        sp_isolate("--path", str(isolated_path1))
    sp_isolate("--overwrite", "--path", str(isolated_path2))
    with open(cfg_dir / "isolate" / "bootstrap.yaml", "r", encoding="utf-8") as f:
        text = f.read().strip()
    expected_text = f"""\
bootstrap:
  root: {isolated_path2 / "bootstrap"}"""
    assert text == expected_text


def test_self_isolate(mock_pre_isolate_config):
    cfg_dir, _ = mock_pre_isolate_config
    sp_isolate("--self")
    assert os.path.exists(spack.cmd.isolate.ISOLATE_SCOPE_PATH)
    assert os.path.exists(spack.cmd.isolate.PRESERVED_INCLUDE_PATH)
    assert os.path.exists(os.path.join(spack.cmd.isolate.ISOLATE_SCOPE_PATH, "bootstrap.yaml"))
    assert os.path.exists(os.path.join(spack.cmd.isolate.ISOLATE_SCOPE_PATH, "config.yaml"))
    # configuration has changed on disk, this refreshes it in memory
    reread_ctx = _reread(cfg_dir)
    sp_config("add", "packages:gcc:buildable:false", ctx=reread_ctx)
    new_config_path = os.path.join(spack.cmd.isolate.ISOLATE_SCOPE_PATH, "packages.yaml")
    assert os.path.exists(new_config_path)
    with open(new_config_path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    expected_text = """\
packages:
  gcc:
    buildable: false"""
    assert text == expected_text


def test_self_isolate_overwrite(mock_pre_isolate_config):
    sp_isolate("--self")
    cfg_dir, _ = mock_pre_isolate_config
    with pytest.raises(Exception):
        sp_isolate("--self")
    new_concr_config_path = os.path.join(spack.cmd.isolate.ISOLATE_SCOPE_PATH, "concretizer.yaml")
    new_pkgs_config_path = os.path.join(spack.cmd.isolate.ISOLATE_SCOPE_PATH, "packages.yaml")
    # configuration has changed on disk, this refreshes it in memory
    reread_ctx = _reread(cfg_dir)
    sp_config("add", "concretizer:reuse:false", ctx=reread_ctx)
    assert os.path.exists(new_concr_config_path)
    with open(new_concr_config_path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    expected_text = """\
concretizer:
  reuse: false"""
    assert text == expected_text
    sp_isolate("--self", "--overwrite")

    reread_ctx = _reread(cfg_dir)
    sp_config("add", "packages:gcc:buildable:false", ctx=reread_ctx)
    assert not os.path.exists(new_concr_config_path)
    assert os.path.exists(new_pkgs_config_path)
    with open(new_pkgs_config_path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    expected_text = """\
packages:
  gcc:
    buildable: false"""
    assert text == expected_text


def test_isolate_undo(mock_pre_isolate_config):
    cfg_dir, iso_root = mock_pre_isolate_config
    isolated_path = iso_root / "test-isolation"
    sp_isolate("--path", str(isolated_path))
    sp_isolate("--undo")
    reread_ctx = _reread(cfg_dir)
    assert "isolate" not in sp_config("scopes", ctx=reread_ctx)
