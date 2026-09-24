# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import pathlib
import shutil

import pytest

import spack.binary_distribution as bd
import spack.concretize
import spack.mirrors.mirror
import spack.test.harness
from spack.installer import PackageInstaller

pytestmark = pytest.mark.not_on_windows("does not run on windows")


def test_build_tarball_overwrite(install_mockery, mock_fetch, monkeypatch, tmp_path: pathlib.Path):
    spec = spack.concretize.concretize_one(
        "trivial-install-test-package", spack.test.harness.current()
    )
    PackageInstaller([spec.package], fake=True).install()

    specs = [spec]
    ctx = spack.test.harness.current()

    def make_uploader(mirror, force=False):
        return bd.make_uploader(mirror, force=force, ctx=ctx)

    # populate cache, everything is new
    mirror = spack.mirrors.mirror.Mirror.from_local_path(str(tmp_path))
    with make_uploader(mirror) as uploader:
        skipped = uploader.push_or_raise(specs)
        assert not skipped

    # should skip all
    with make_uploader(mirror) as uploader:
        skipped = uploader.push_or_raise(specs)
        assert skipped == specs

    # with force=True none should be skipped
    with make_uploader(mirror, force=True) as uploader:
        skipped = uploader.push_or_raise(specs)
        assert not skipped

    # Remove the tarball, which should cause push to push.
    shutil.rmtree(tmp_path / bd.buildcache_relative_blobs_path())

    with make_uploader(mirror) as uploader:
        skipped = uploader.push_or_raise(specs)
        assert not skipped
