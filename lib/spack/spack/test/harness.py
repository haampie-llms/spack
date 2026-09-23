# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""The context tests run in, and the means fixtures have to change it.

Tests share one :class:`~spack.context.SpackContext`, returned by :func:`current`. Fixtures
swap its configuration, store and repositories within context managers, so code holding the
context sees the swaps, as it would see changes to the process state.
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

_CONTEXT: Optional[spack.context.SpackContext] = None


def current() -> spack.context.SpackContext:
    """Return the context of the running test."""
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = spack.context.SpackContext(spack.config.create())
    return _CONTEXT


def set_current(ctx: spack.context.SpackContext) -> None:
    """Make ``ctx`` the context of the running test, e.g. in a child process."""
    global _CONTEXT
    _CONTEXT = ctx


#: Members built from the configuration alone, dropped whenever the configuration changes
_CONFIG_DERIVED = ("network", "binary_index", "gpg", "bootstrap")


def reset_derived_members() -> None:
    """Rebuild the members derived from the configuration of the test context on next access."""
    ctx = current()
    for member in _CONFIG_DERIVED:
        ctx.__dict__.pop(member, None)


@contextlib.contextmanager
def use_configuration(
    *scopes_or_paths: Union[spack.config.ScopeWithOptionalPriority, str],
) -> Generator[spack.config.Configuration, None, None]:
    """Use a configuration made of the given scopes within the context manager."""
    ctx = current()
    configuration = spack.config.create_from(*scopes_or_paths)
    saved = ctx._config
    ctx._config = configuration
    reset_derived_members()
    try:
        yield configuration
    finally:
        ctx._config = saved
        reset_derived_members()


@contextlib.contextmanager
def use_configuration_and_store(
    *scopes_or_paths: Union[spack.config.ScopeWithOptionalPriority, str],
) -> Generator[spack.config.Configuration, None, None]:
    """Like ``use_configuration``, with a store built from the new configuration."""
    ctx = current()
    with use_configuration(*scopes_or_paths) as configuration:
        saved = ctx.swap("store", None)
        try:
            yield configuration
        finally:
            ctx.swap("store", saved)


@contextlib.contextmanager
def use_store(
    path: Union[str, pathlib.Path], extra_data: Optional[Dict[str, Any]] = None
) -> Generator[spack.store.Store, None, None]:
    """Use the store at ``path`` within the context manager. ``extra_data`` are extra settings
    under ``config:install_tree``."""
    ctx = current()
    assert not isinstance(path, spack.store.Store), "cannot pass a store anymore"
    scope_name = f"use-store-{uuid.uuid4()}"
    data = {"root": str(path)}
    if extra_data:
        data.update(extra_data)

    config = ctx.config
    config.push_scope(
        spack.config.InternalConfigScope(name=scope_name, data={"config": {"install_tree": data}})
    )
    store = spack.store.create(config, repo_provider=ctx.repo_provider)
    saved = ctx.swap("store", store)
    try:
        yield store
    finally:
        ctx.swap("store", saved)
        config.remove_scope(scope_name=scope_name)


@contextlib.contextmanager
def use_repositories(
    *paths_and_repos: Union[str, spack.repo.Repo], override: bool = True
) -> Generator[spack.repo.RepoPath, None, None]:
    """Use the repositories passed as arguments within the context manager.

    ``Repo`` instances are used as-is; paths are constructed into fresh ``Repo`` instances,
    with ``package_attributes`` overrides from the current configuration applied.

    Args:
        *paths_and_repos: paths to the repositories to be used, or
            already constructed Repo objects
        override: if True use only the repositories passed as input,
            if False add them to the top of the list of current repositories.
    """
    ctx = current()
    config = ctx.config
    old_repo = ctx.repo
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
        new_repos.extend(r for r in old_repo.repos if r.root not in paths)
    new_repo = spack.repo.RepoPath(*new_repos)
    # The scope keeps the repos section in sync with the repositories: child processes and
    # environment activation read it.
    scope_name = f"use-repo-{uuid.uuid4()}"
    repos_key = "repos:" if override else "repos"
    config.push_scope(spack.config.InternalConfigScope(name=scope_name, data={repos_key: paths}))
    old_repo.disable()
    ctx.swap("repo", new_repo)
    try:
        yield new_repo
    finally:
        new_repo.disable()
        ctx.swap("repo", old_repo)
        config.remove_scope(scope_name=scope_name)


@contextlib.contextmanager
def use_platform(new_platform: spack.platforms.Platform):
    """Use ``new_platform`` as the host platform within the context manager."""
    assert isinstance(new_platform, spack.platforms.Platform), f'"{new_platform}" is no Platform'
    original = spack.platforms.host
    spack.platforms.host = spack.platforms._PickleableCallable(new_platform)
    spack.subprocess_context.MONKEYPATCHES.append((spack.platforms, "host"))
    # Configuration scopes and caches depend on the platform
    current().config.clear_caches()
    try:
        yield new_platform
    finally:
        spack.platforms.host = original
        current().config.clear_caches()


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
