# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""The resources an operation reads, built from one configuration.

A :class:`SpackContext` holds a configuration and builds everything derived from it on first
access, so an operation only pays for what it reads.

This module imports nothing at runtime, so it can be imported from anywhere.
"""

from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar, overload

if TYPE_CHECKING:
    import spack.binary_distribution
    import spack.compilers.libraries
    import spack.config
    import spack.environment
    import spack.repo
    import spack.store
    import spack.util.file_cache
    import spack.util.web

T = TypeVar("T")


class _member(Generic[T]):
    """``functools.cached_property`` for Python 3.6 and 3.7: the value is built on first access
    and stored on the instance."""

    def __init__(self, build: Callable[[Any], T]) -> None:
        self.build = build
        self.name = build.__name__
        self.__doc__ = build.__doc__

    @overload
    def __get__(self, instance: None, owner: Any = None) -> "_member[T]": ...

    @overload
    def __get__(self, instance: object, owner: Any = None) -> T: ...

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        value = instance.__dict__[self.name] = self.build(instance)
        return value


class SpackContext:
    """External resources an operation reads, all derived from ``config``."""

    def __init__(
        self,
        config: "spack.config.Configuration",
        *,
        environment: Optional["spack.environment.Environment"] = None,
    ) -> None:
        self._config = config
        self._environment = environment

    @property
    def config(self) -> "spack.config.Configuration":
        """Layered configuration driving the operation."""
        return self._config

    @property
    def environment(self) -> Optional["spack.environment.Environment"]:
        """The environment of the operation, whose scope is part of ``config``."""
        return self._environment

    @_member
    def misc_cache(self) -> "spack.util.file_cache.FileCache":
        """Cache for small data (package indexes, ...)."""
        import spack.caches

        return spack.caches.misc_cache(config=self.config)

    @_member
    def store(self) -> "spack.store.Store":
        """Installed-spec store."""
        import spack.store

        return spack.store.create(self.config)

    @_member
    def repo(self) -> "spack.repo.RepoPath":
        """Package repositories, enabled for importing package modules."""
        import spack.repo

        return spack.repo.create_and_enable(self.config, cache=self.misc_cache)

    @_member
    def binary_index(self) -> "spack.binary_distribution.BinaryIndexCache":
        """Buildcache index."""
        import spack.binary_distribution

        return spack.binary_distribution.BinaryIndexCache(config=self.config, client=self.network)

    @_member
    def compiler_cache(self) -> "spack.compilers.libraries.CompilerCache":
        """Cache for compiler output (implicit rpaths, libc, ...)."""
        import spack.compilers.libraries

        return spack.compilers.libraries.FileCompilerCache(self.misc_cache)

    @_member
    def network(self) -> "spack.util.web.NetworkClient":
        """Network settings and the URL opener built from them."""
        import spack.util.web

        return spack.util.web.NetworkClient.from_config(self.config)

    def __reduce__(self):
        return SpackContext, (self._config,), {"_environment": self._environment}

    def __setstate__(self, state):
        self.__dict__.update(state)


class _ProcessContext(SpackContext):
    """A context whose members are the process globals, read at each access.

    This is a transitional view: it goes away together with the globals.
    """

    def __init__(self) -> None:
        pass

    @property
    def config(self) -> "spack.config.Configuration":
        import spack.config

        return spack.config.CONFIG

    @property
    def environment(self) -> Optional["spack.environment.Environment"]:
        from spack.active_environment import active_environment

        return active_environment()

    @property  # type: ignore[override]
    def misc_cache(self) -> "spack.util.file_cache.FileCache":
        import spack.caches

        return spack.caches.MISC_CACHE

    @property  # type: ignore[override]
    def store(self) -> "spack.store.Store":
        import spack.store

        return spack.store.STORE

    @property  # type: ignore[override]
    def repo(self) -> "spack.repo.RepoPath":
        import spack.repo

        return spack.repo.PATH

    @property  # type: ignore[override]
    def binary_index(self) -> "spack.binary_distribution.BinaryIndexCache":
        import spack.binary_distribution

        return spack.binary_distribution.BINARY_INDEX

    @property  # type: ignore[override]
    def compiler_cache(self) -> "spack.compilers.libraries.CompilerCache":
        import spack.compilers.libraries

        return spack.compilers.libraries.process_compiler_cache()

    def __reduce__(self):
        return _ProcessContext, ()


def default() -> SpackContext:
    """Return a view of the process globals as a context (transitional)."""
    return _ProcessContext()
