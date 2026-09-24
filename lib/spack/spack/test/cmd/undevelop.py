# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import pathlib

import spack.concretize
import spack.environment as ev
from spack.context import SpackContext
from spack.test.harness import SpackCommand
from spack.util.filesystem import working_dir

undevelop = SpackCommand("undevelop")
env = SpackCommand("env")
concretize = SpackCommand("concretize")


def test_undevelop(
    tmp_path: pathlib.Path, mutable_config, mock_packages, mutable_mock_env_path, ctx: SpackContext
):
    # setup environment
    envdir = tmp_path / "env"
    envdir.mkdir()
    with working_dir(str(envdir)):
        with open("spack.yaml", "w", encoding="utf-8") as f:
            f.write(
                """\
spack:
  specs:
  - mpich

  develop:
    mpich:
      spec: mpich@1.0
      path: /fake/path
"""
            )

        env("create", "test", "./spack.yaml")
        with ev.read("test", ctx=ctx):
            before = spack.concretize.concretize_one("mpich", ctx)
            undevelop("mpich")
            after = spack.concretize.concretize_one("mpich", ctx)

    # Removing dev spec from environment changes concretization
    assert before.satisfies("dev_path=*")
    assert not after.satisfies("dev_path=*")


def test_undevelop_all(
    tmp_path: pathlib.Path, mutable_config, mock_packages, mutable_mock_env_path, ctx: SpackContext
):
    # setup environment
    envdir = tmp_path / "env"
    envdir.mkdir()
    with working_dir(str(envdir)):
        with open("spack.yaml", "w", encoding="utf-8") as f:
            f.write(
                """\
spack:
  specs:
  - mpich

  develop:
    mpich:
      spec: mpich@1.0
      path: /fake/path
"""
            )

        env("create", "test", "./spack.yaml")
        with ev.read("test", ctx=ctx):
            before = spack.concretize.concretize_one("mpich", ctx)
            undevelop("--all")
            after = spack.concretize.concretize_one("mpich", ctx)

    # Removing dev spec from environment changes concretization
    assert before.satisfies("dev_path=*")
    assert not after.satisfies("dev_path=*")


def test_undevelop_nonexistent(
    tmp_path: pathlib.Path, mutable_config, mock_packages, mutable_mock_env_path, ctx: SpackContext
):
    # setup environment
    envdir = tmp_path / "env"
    envdir.mkdir()
    with working_dir(str(envdir)):
        with open("spack.yaml", "w", encoding="utf-8") as f:
            f.write(
                """\
spack:
  specs:
  - mpich

  develop:
    mpich:
      spec: mpich@1.0
      path: /fake/path
"""
            )

        env("create", "test", "./spack.yaml")
        with ev.read("test", ctx=ctx) as e:
            concretize()
            before = e.specs_by_hash
            undevelop("package-not-in-develop")  # does nothing
            concretize("-f")
            after = e.specs_by_hash

    # nothing should have changed
    assert before == after
