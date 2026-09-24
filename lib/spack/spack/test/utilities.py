# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Non-fixture utilities for test code. Must be imported."""

import contextlib
import pathlib
import uuid
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

import spack.config
import spack.context
import spack.platforms
import spack.repo
import spack.store
from spack.concretize_ui import ConcretizerUI, SolveKind
from spack.main import make_argument_parser
from spack.spec import Spec


class SpackCommandArgs:
    """Use this to get an Args object like what is passed into
    a command.

    Useful for emulating args in unit tests that want to check
    helper functions in Spack commands. Ensures that you get all
    the default arg values established by the parser.

    Example usage::

        install_args = SpackCommandArgs("install")("-v", "mpich")
    """

    def __init__(self, command_name):
        self.parser = make_argument_parser()
        self.command_name = command_name

    def __call__(self, *argv, **kwargs):
        self.parser.config = spack.context.default().config
        self.parser.add_command(self.command_name)
        args, unknown = self.parser.parse_known_args([self.command_name] + list(argv))
        return args


class RecordingUI(ConcretizerUI):
    """Concretizer frontend that records the events it receives, instead of rendering them.

    Each list holds the arguments of the corresponding callback, in the order they were received.

    Example usage::

        ui = RecordingUI()
        spack.concretize.concretize_spec_pairs([(Spec("pkg-a"), None)], ui=ui)
        assert ui.groups == [("default", SolveKind.TOGETHER, 1, 1)]
    """

    def __init__(self) -> None:
        #: how many concretizations started, and how many of them reported their end
        self.started = 0
        self.ended = 0
        #: (group, kind, total, processes) for each group that started
        self.groups: List[Tuple[str, SolveKind, int, int]] = []
        #: how many groups reported their end
        self.groups_ended = 0
        #: (abstract, concrete, count, duration) for each spec that was concretized
        self.concretized: List[Tuple[Spec, Spec, int, float]] = []

    def on_concretization_started(self) -> None:
        self.started += 1

    def on_concretization_finished(self) -> None:
        self.ended += 1

    def on_group_started(self, *, group: str, kind: SolveKind, total: int, processes: int) -> None:
        self.groups.append((group, kind, total, processes))

    def on_group_finished(self) -> None:
        self.groups_ended += 1

    def on_spec_concretized(
        self, abstract: Spec, *, concrete: Spec, count: int, duration: float
    ) -> None:
        self.concretized.append((abstract, concrete, count, duration))


class UnusableGlobal:
    """Stands in for a process global that the code under test must not reach for."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item):
        # pickle looks up optional dunder methods, and on load it does so before _name is set
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        raise AssertionError(
            f"{self._name} was read instead of the injected context (attribute {item!r})"
        )


@contextlib.contextmanager
def use_store(
    path: Union[str, pathlib.Path], extra_data: Optional[Dict[str, Any]] = None
) -> Generator[spack.store.Store, None, None]:
    """Use the store at ``path`` in the process context within the context manager.
    ``extra_data`` are extra settings under ``config:install_tree``."""
    ctx = spack.context.default()
    scope_name = f"use-store-{uuid.uuid4()}"
    data = {"root": str(path)}
    if extra_data:
        data.update(extra_data)

    config = ctx.config
    config.push_scope(
        spack.config.InternalConfigScope(name=scope_name, data={"config": {"install_tree": data}})
    )
    store = spack.store.create(config, repo_provider=ctx.repo_provider)
    saved = ctx.__dict__.pop("store", None)
    ctx.__dict__["store"] = store
    try:
        yield store
    finally:
        ctx.__dict__.pop("store", None)
        if saved is not None:
            ctx.__dict__["store"] = saved
        config.remove_scope(scope_name=scope_name)


@contextlib.contextmanager
def use_repositories(
    *paths_and_repos: Union[str, spack.repo.Repo], override: bool = True
) -> Generator[spack.repo.RepoPath, None, None]:
    """Use the repositories passed as arguments in the process context within the context
    manager.

    ``Repo`` instances are used as-is; paths are constructed into fresh ``Repo`` instances,
    with ``package_attributes`` overrides from the configuration applied.

    Args:
        *paths_and_repos: paths to the repositories to be used, or
            already constructed Repo objects
        override: if True use only the repositories passed as input,
            if False add them to the top of the list of current repositories.
    Returns:
        Corresponding RepoPath object
    """
    ctx = spack.context.default()
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
    # The scope keeps the repos config section in sync with the enabled repositories: child
    # processes and environment activation read it.
    scope_name = f"use-repo-{uuid.uuid4()}"
    repos_key = "repos:" if override else "repos"
    config.push_scope(spack.config.InternalConfigScope(name=scope_name, data={repos_key: paths}))
    old_repo.disable()
    ctx.__dict__["repo"] = new_repo
    new_repo.enable()
    try:
        yield new_repo
    finally:
        config.remove_scope(scope_name=scope_name)
        new_repo.disable()
        ctx.__dict__["repo"] = old_repo
        old_repo.enable()


@contextlib.contextmanager
def use_configuration(
    *scopes_or_paths: Union["spack.config.ScopeWithOptionalPriority", str],
) -> Generator[spack.config.Configuration, None, None]:
    """Make a context with the configuration of the scopes passed as arguments the context of the
    process within the context manager."""
    config = spack.config.create_from(*scopes_or_paths)
    previous = spack.context.set_default(spack.context.SpackContext(config))
    try:
        yield config
    finally:
        spack.context.set_default(previous)


@contextlib.contextmanager
def use_platform(
    new_platform: spack.platforms.Platform,
) -> Generator[spack.platforms.Platform, None, None]:
    """Use ``new_platform`` as the host platform within the context manager."""
    assert isinstance(new_platform, spack.platforms.Platform), f'"{new_platform}" is no Platform'
    original = spack.platforms.host
    spack.platforms.host = spack.platforms._PickleableCallable(new_platform)
    # Configuration scopes and caches depend on the platform
    spack.context.default().config.clear_caches()
    try:
        yield new_platform
    finally:
        spack.platforms.host = original
        spack.context.default().config.clear_caches()
