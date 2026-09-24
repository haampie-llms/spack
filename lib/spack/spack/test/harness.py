# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Helpers for the context each test runs in.

Every test that needs one gets its own :class:`~SpackContext` from the ``ctx``
fixture. Fixtures set up that context by pushing configuration scopes and replacing members; as
the context is discarded after the test, nothing has to be undone.

:func:`current` returns the context of the running test, for the code that cannot receive it as
an argument: :class:`SpackCommand` and processes started with :class:`SpackTestProcess`.
"""

import contextlib
import pathlib
import uuid
from typing import Any, Dict, Generator, Optional, Union

import spack.config
import spack.context
import spack.main
import spack.platforms
import spack.repo
import spack.store
import spack.subprocess_context
from spack.context import SpackContext

_CONTEXT: Optional[SpackContext] = None


def current() -> SpackContext:
    """Return the context of the running test."""
    if _CONTEXT is None:
        raise RuntimeError("the test has no Spack context: request the `ctx` fixture")
    return _CONTEXT


def set_current(ctx: Optional[SpackContext]) -> None:
    """Make ``ctx`` the context of the running test, or unset it with ``None``. It is the context
    of the process too, which the package API reads outside of package code (transitional)."""
    global _CONTEXT
    _CONTEXT = ctx
    spack.context.set_default(ctx)


def set_store(
    ctx: SpackContext, path: Union[str, pathlib.Path], extra_data: Optional[Dict[str, Any]] = None
) -> spack.store.Store:
    """Make the store at ``path`` the store of ``ctx``. ``extra_data`` are extra settings under
    ``config:install_tree``. Returns the new store."""
    data: Dict[str, Any] = {"root": str(path)}
    if extra_data:
        data.update(extra_data)
    ctx.config.push_scope(
        spack.config.InternalConfigScope(
            name=f"store-{uuid.uuid4()}", data={"config": {"install_tree": data}}
        )
    )
    store = spack.store.create(ctx.config, repo_provider=ctx.repo_provider)
    ctx.swap("store", store)
    return store


def set_repositories(
    ctx: SpackContext, *paths_and_repos: Union[str, spack.repo.Repo], override: bool = True
) -> spack.repo.RepoPath:
    """Make the given repositories the repositories of ``ctx``, and return them.

    ``Repo`` instances are used as-is; paths are constructed into fresh ``Repo`` instances, with
    ``package_attributes`` overrides from the configuration of ``ctx`` applied.

    Args:
        ctx: context whose repositories are replaced
        *paths_and_repos: paths to the repositories, or constructed ``Repo`` objects
        override: if True use only the repositories passed as input, if False put them in front
            of the current repositories
    """
    config = ctx.config
    overrides = spack.repo.package_attributes_overrides(config)
    new_repos = [
        x
        if isinstance(x, spack.repo.Repo)
        else spack.repo.Repo(
            spack.config.canonicalize_path(x, config=config),
            cache=ctx.misc_cache,
            overrides=overrides,
        )
        for x in paths_and_repos
    ]
    paths = {r.root: r.root for r in new_repos}
    if not override:
        new_repos.extend(r for r in ctx.repo.repos if r.root not in paths)
    repo = spack.repo.RepoPath(*new_repos)
    # The scope keeps the repos section in sync with the repositories: child processes and
    # environment activation read it.
    repos_key = "repos:" if override else "repos"
    config.push_scope(
        spack.config.InternalConfigScope(name=f"repos-{uuid.uuid4()}", data={repos_key: paths})
    )
    ctx.swap("repo", repo)
    return repo


@contextlib.contextmanager
def _restoring(ctx: SpackContext, member: str) -> Generator[None, None, None]:
    """Restore the configuration scopes and ``member`` of ``ctx`` on exit."""
    scopes = set(ctx.config.scopes)
    saved = ctx.__dict__.get(member)
    try:
        yield
    finally:
        for name in [name for name in ctx.config.scopes if name not in scopes]:
            ctx.config.remove_scope(name)
        ctx.swap(member, saved)


@contextlib.contextmanager
def use_store(
    ctx: SpackContext, path: Union[str, pathlib.Path], extra_data: Optional[Dict[str, Any]] = None
) -> Generator[spack.store.Store, None, None]:
    """Like :func:`set_store`, restoring the previous store on exit."""
    with _restoring(ctx, "store"):
        yield set_store(ctx, path, extra_data)


@contextlib.contextmanager
def use_repositories(
    ctx: SpackContext, *paths_and_repos: Union[str, spack.repo.Repo], override: bool = True
) -> Generator[spack.repo.RepoPath, None, None]:
    """Like :func:`set_repositories`, restoring the previous repositories on exit."""
    with _restoring(ctx, "repo"):
        yield set_repositories(ctx, *paths_and_repos, override=override)


@contextlib.contextmanager
def use_platform(new_platform: spack.platforms.Platform):
    """Use ``new_platform`` as the host platform within the context manager."""
    assert isinstance(new_platform, spack.platforms.Platform), f'"{new_platform}" is no Platform'
    original = spack.platforms.host
    spack.platforms.host = spack.platforms._PickleableCallable(new_platform)
    spack.subprocess_context.MONKEYPATCHES.append((spack.platforms, "host"))
    # Configuration scopes and caches depend on the platform
    if _CONTEXT is not None:
        _CONTEXT.config.clear_caches()
    try:
        yield new_platform
    finally:
        spack.platforms.host = original
        if _CONTEXT is not None:
            _CONTEXT.config.clear_caches()


class SpackCommand(spack.main.SpackCommand):
    """Run a Spack command in the context of the running test."""

    def __call__(self, *argv: str, ctx=None, **kwargs) -> str:  # type: ignore[override]
        return super().__call__(*argv, ctx=ctx or current(), **kwargs)


class SpackTestProcess:
    """A process running ``fn`` in the context of the running test."""

    def __init__(self, fn):
        self.fn = fn

    @staticmethod
    def _restore_and_run(fn, ctx, test_patches):
        set_current(ctx)
        test_patches.restore()
        fn()

    def create(self):
        import multiprocessing

        return multiprocessing.Process(
            target=self._restore_and_run,
            args=(self.fn, current(), spack.subprocess_context.TestPatches.create()),
        )
