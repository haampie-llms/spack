# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Caches used by Spack to store data"""

import spack.config
import spack.fetch_strategy
import spack.paths
import spack.util.file_cache


def misc_cache_location(*, config: spack.config.Configuration) -> str:
    """Location of Spack's cache for small data.

    Currently the misc cache stores indexes for virtual dependency
    providers and for which packages provide which tags.
    """
    path = config.get("config:misc_cache", spack.paths.default_misc_cache_path)
    return spack.config.canonicalize_path(path, config=config)


def misc_cache(*, config: spack.config.Configuration) -> spack.util.file_cache.FileCache:
    """Return a ``FileCache`` rooted at the misc-cache location derived from ``config``."""
    return spack.util.file_cache.FileCache(
        misc_cache_location(config=config), enable_lock=config.get("config:locks", True)
    )


def fetch_cache_location(*, config: spack.config.Configuration) -> str:
    """Filesystem cache of downloaded archives.

    This prevents Spack from repeatedly fetch the same files when
    building the same package different ways or multiple times.
    """
    path = config.get("config:source_cache")
    if not path:
        path = spack.paths.default_fetch_cache_path
    return spack.config.canonicalize_path(path, config=config)


def fetch_cache(config: spack.config.Configuration) -> spack.fetch_strategy.FsCache:
    """Returns Spack's local cache for downloaded source archives, as configured in ``config``."""
    return spack.fetch_strategy.FsCache(fetch_cache_location(config=config))


class MirrorCache(spack.fetch_strategy.FsCacheBase):
    def __init__(self, root, skip_unstable_versions):
        super().__init__(root)
        self.skip_unstable_versions = skip_unstable_versions

    def store(self, fetcher, relative_dest):
        """Fetch and relocate the fetcher's target into our mirror cache.

        Note: archives package sources even if not normally cached (e.g. tip of hg/git branch).
        """
        super().store(fetcher, relative_dest)
