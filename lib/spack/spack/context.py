# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""The resources an operation reads, built from one configuration.

A :class:`SpackContext` holds a configuration and builds everything derived from it on first
access, so an operation only pays for what it reads. Command entry points receive one from
``spack.main``, and pass the instances their callees need.

This module imports nothing at runtime, so it can be imported from anywhere.
"""

from typing import TYPE_CHECKING, Any, Callable, Dict, Generic, Optional, TypeVar, overload

if TYPE_CHECKING:
    import spack.binary_distribution
    import spack.compilers.libraries
    import spack.config
    import spack.environment
    import spack.relocate
    import spack.repo
    import spack.store
    import spack.util.executable
    import spack.util.file_cache
    import spack.util.gpg
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
        self, config: "spack.config.Configuration", *, is_bootstrap: bool = False
    ) -> None:
        self._config = config
        self._environment: Optional["spack.environment.Environment"] = None
        #: Whether this context bootstraps Spack's own dependencies
        self.is_bootstrap = is_bootstrap
        #: Members replaced by activating an environment, restored by deactivating it
        self._before_activation: Dict[str, Any] = {}
        #: Error reading the environment to activate, if its manifest is broken
        self.environment_error: Optional[Exception] = None
        #: GnuPG home of ``gpg``; ``None`` for ``SPACK_GNUPGHOME``, or Spack's own
        self.gpg_home: Optional[str] = None

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

        return spack.store.create(self.config, repo_provider=self.repo_provider)

    @_member
    def repo(self) -> "spack.repo.RepoPath":
        """Package repositories, enabled for importing package modules."""
        import spack.repo

        repo = spack.repo.RepoPath.from_config(self.config, cache=self.misc_cache)
        repo.enable()
        return repo

    def repo_provider(self) -> "spack.repo.RepoPath":
        """Return ``repo``: pass the bound method where the repositories may be needed later."""
        return self.repo

    @_member
    def binary_index(self) -> "spack.binary_distribution.BinaryIndexCache":
        """Buildcache index."""
        import spack.binary_distribution

        return spack.binary_distribution.BinaryIndexCache(
            config=self.config, client=self.network, repo_provider=self.repo_provider
        )

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

    @_member
    def gpg(self) -> "spack.util.gpg.Gpg":
        """GnuPG, to sign and verify binaries."""
        import spack.util.gpg

        return spack.util.gpg.Gpg(self.gpg_home, self)

    @_member
    def bootstrap(self) -> "SpackContext":
        """Context to bootstrap Spack's own dependencies in."""
        if self.is_bootstrap:
            return self
        import spack.bootstrap.config

        return spack.bootstrap.config.bootstrap_context(self)

    def environment_dir(self, name_or_dir: str) -> str:
        """Directory of the environment with the given name, or in the given directory."""
        import spack.environment

        return spack.environment.as_env_dir(name_or_dir, config=self.config)

    def read_environment(self, name_or_dir: str) -> "spack.environment.Environment":
        """Read the environment with the given name, or in the given directory."""
        import spack.environment

        return spack.environment.environment_from_name_or_dir(name_or_dir, ctx=self)

    def ensure_clingo(self) -> None:
        """Make the clingo module importable, bootstrapping it if needed."""
        import spack.bootstrap

        spack.bootstrap.ensure_clingo_importable_or_raise(self)

    def ensure_patchelf(self) -> "spack.util.executable.Executable":
        """Return patchelf, bootstrapping it if needed."""
        import spack.bootstrap

        return spack.bootstrap.ensure_patchelf_in_path_or_raise(self)

    @_member
    def patchelf(self) -> "spack.relocate.PatchelfFinder":
        """Finds patchelf on its first call, bootstrapping it if needed."""
        import spack.relocate

        return spack.relocate.patchelf_finder(self)

    def ensure_gpg(self) -> "spack.util.executable.Executable":
        """Return gpg, bootstrapping it if needed."""
        import spack.bootstrap

        return spack.bootstrap.ensure_gpg_in_path_or_raise(self)

    def ensure_windows_sdk(self) -> None:
        """Add the Windows SDK and WGL to the configuration as externals, if missing."""
        import spack.bootstrap

        spack.bootstrap.ensure_winsdk_external_or_raise(self)

    def share(self, other: "SpackContext", *members: str) -> None:
        """Use the given members of ``other`` instead of building them from ``config``."""
        for member in members:
            self.__dict__[member] = getattr(other, member)

    def activate(
        self, env: "spack.environment.Environment", *, use_env_repo: bool = False
    ) -> None:
        """Make ``env`` the environment of this context: its scope is pushed onto ``config``, and
        the store and repositories are rebuilt if the scope changes their configuration."""
        self.deactivate()
        before = self._store_and_repo_config()
        # Set first: "$env" substitutions in the environment's includes need it
        self._set_environment(env)
        try:
            env.manifest.prepare_config_scope(self.config)
        except Exception:
            self._set_environment(None)
            raise
        after = self._store_and_repo_config()
        if before[0] != after[0]:
            self._before_activation["store"] = self.swap("store", None)
        if before[1] != after[1] or use_env_repo:
            import spack.repo

            repo = spack.repo.RepoPath.from_config(self.config, cache=self.misc_cache)
            if use_env_repo:
                repo.put_first(env.repo)
            self._before_activation["repo"] = self.swap("repo", repo)

    def deactivate(self) -> None:
        """Undo ``activate``, if an environment is active."""
        env = self.environment
        if env is None:
            return
        for member, value in self._before_activation.items():
            self.swap(member, value)
        self._before_activation.clear()
        env.manifest.deactivate_config_scope(self.config)
        self._set_environment(None)

    def _store_and_repo_config(self):
        config = self.config
        return ((config.get("config:install_tree"), config.get("upstreams")), config.get("repos"))

    def _set_environment(self, env: Optional["spack.environment.Environment"]) -> None:
        self._environment = env
        self.config.env_path = env.path if env is not None else None

    def swap(self, member: str, value: Any) -> Any:
        """Replace a member, and return the previous one if it was built. ``None`` rebuilds it
        on next access."""
        previous = self.__dict__.pop(member, None)
        if value is not None:
            self.__dict__[member] = value
            if member == "repo":
                value.enable()
        return previous

    def __reduce__(self):
        # Not the environment, which is large: its scope and path are part of config
        return (
            SpackContext,
            (self._config,),
            {"is_bootstrap": self.is_bootstrap, "gpg_home": self.gpg_home},
        )
