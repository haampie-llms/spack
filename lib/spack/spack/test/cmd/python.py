# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import platform
import sys

import pytest

import spack
import spack.paths
from spack.main import SpackCommand
from spack.util.executable import Executable

python = SpackCommand("python")


def test_python():
    out = python("-c", "import spack; print(spack.spack_version)")
    assert out.strip() == spack.spack_version


def test_python_interpreter_path():
    out = python("--path")
    assert out.strip() == sys.executable


def test_python_version():
    out = python("-V")
    assert platform.python_version() in out


def test_python_with_module():
    # pytest rewrites a lot of modules, which interferes with runpy, so
    # it's hard to test this.  Trying to import a module like sys, that
    # has no code associated with it, raises an error reliably in python
    # 2 and 3, which indicates we successfully ran runpy.run_module.
    with pytest.raises(ImportError, match="No code object"):
        python("-m", "sys")


def test_python_finalizes_objects_at_exit(tmp_path):
    """Objects of spack python -c are freed before bin/spack skips garbage collection at exit."""
    scope = tmp_path / "scope"
    scope.mkdir()
    repos = f"repos::\n  builtin_mock: {spack.paths.mock_packages_path}\n"
    (scope / "repos.yaml").write_text(repos)
    out = tmp_path / "out.txt"
    # the buffered write only reaches the file when the file object is finalized
    Executable(sys.executable)(
        spack.paths.spack_script,
        "-C",
        str(scope),
        "python",
        "-c",
        f"f = open({str(out)!r}, 'w'); f.write('data')",
        extra_env={
            "SPACK_DISABLE_LOCAL_CONFIG": "1",
            "SPACK_USER_CONFIG_PATH": str(tmp_path / "user_config"),
            "SPACK_USER_CACHE_PATH": str(tmp_path / "user_cache"),
        },
    )
    assert out.read_text() == "data"


def test_python_raises():
    out = python("--foobar", fail_on_error=False)
    assert python.returncode == 2
    assert "--foobar" in out
