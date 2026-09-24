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
