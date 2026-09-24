# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import pathlib

import spack.fetch_strategy as spack_fs
import spack.stage as spack_stage
from spack.context import SpackContext


def test_s3fetchstrategy_downloaded(tmp_path: pathlib.Path, config, ctx: SpackContext):
    """Ensure fetch with archive file already downloaded is a noop."""
    archive = tmp_path / "s3.tar.gz"

    class Archived_S3FS(spack_fs.S3FetchStrategy):
        @property
        def archive_file(self):
            return archive

    fetcher = Archived_S3FS(url="s3://example/s3.tar.gz")
    with spack_stage.stage_from_config(
        fetcher, path=str(tmp_path), config=config, client=ctx.network
    ):
        fetcher.fetch()
