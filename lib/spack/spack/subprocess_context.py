# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""
This module handles transmission of Spack state to child processes started
using the ``"spawn"`` start method. Notably, installations are performed in a
subprocess and require transmitting the Package object (in such a way
that the repository is available for importing when it is deserialized);
installations performed in Spack unit tests may include additional
modifications to global state in memory that must be replicated in the
child process.
"""

import importlib
import io
import multiprocessing
import multiprocessing.context
import pickle
from types import ModuleType
from typing import TYPE_CHECKING, Optional, Union

import spack.paths
import spack.platforms

if TYPE_CHECKING:
    import spack.package_base

#: Used in tests to track monkeypatches that need to be restored in child processes
MONKEYPATCHES: list = []


def serialize(obj) -> io.BytesIO:
    serialized_obj = io.BytesIO()
    pickle.dump(obj, serialized_obj)
    serialized_obj.seek(0)
    return serialized_obj


class PackageInstallContext:
    """Captures the in-memory process state of a package installation that needs to be transmitted
    to a child process."""

    def __init__(
        self,
        pkg: "spack.package_base.PackageBase",
        *,
        ctx: Optional[multiprocessing.context.BaseContext] = None,
    ):
        ctx = ctx or multiprocessing.get_context()
        self.global_state = GlobalStateMarshaler(ctx=ctx)
        self.pkg: Union["spack.package_base.PackageBase", io.BytesIO] = pkg
        if ctx.get_start_method() != "fork":
            # The context goes first: its repositories import the package class on unpickling.
            # One pickler, so that the package refers to the same context.
            stream = io.BytesIO()
            pickler = pickle.Pickler(stream)
            pickler.dump(pkg.context)
            pickler.dump(pkg)
            stream.seek(0)
            self.pkg = stream

    def restore(self) -> "spack.package_base.PackageBase":
        self.global_state.restore()
        if not isinstance(self.pkg, io.BytesIO):
            return self.pkg
        import spack.repo

        unpickler = pickle.Unpickler(self.pkg)
        ctx = unpickler.load()
        ctx.repo  # enable the repositories before the package is unpickled
        pkg = unpickler.load()
        pkg.spec._package = pkg
        # The dependencies come without packages, which setting up the build environment reads
        spack.repo.attach_packages([pkg.spec], ctx)
        return pkg


class GlobalStateMarshaler:
    """Class to serialize and restore the process state that child processes need, and that is
    not part of the context they receive: the platform, the working directory and, in tests,
    monkeypatches.
    """

    def __init__(self, *, ctx: Optional[multiprocessing.context.BaseContext] = None) -> None:
        ctx = ctx or multiprocessing.get_context()
        self.is_forked = ctx.get_start_method() == "fork"
        if self.is_forked:
            return

        self.platform = spack.platforms.host
        self.test_patches = TestPatches.create()
        self.spack_working_dir = spack.paths.spack_working_dir

    def restore(self):
        if self.is_forked:
            # Erase cached SSL contexts / boto3 clients, since OpenSSL and botocore
            # connection pools are not fork-safe.
            from spack.util import web
            from spack.util.s3 import s3_client_cache

            web.clear_ssl_contexts()
            s3_client_cache.clear()
            return
        spack.platforms.host = self.platform
        spack.paths.spack_working_dir = self.spack_working_dir
        self.test_patches.restore()


class TestPatches:
    def __init__(self, module_patches, class_patches):
        self.module_patches = [(x, y, serialize(z)) for (x, y, z) in module_patches]
        self.class_patches = [(x, y, serialize(z)) for (x, y, z) in class_patches]

    def restore(self):
        if not self.module_patches and not self.class_patches:
            return
        # this code path is only followed in tests, so use inline imports
        from pydoc import locate

        for module_name, attr_name, value in self.module_patches:
            value = pickle.load(value)
            module = importlib.import_module(module_name)
            setattr(module, attr_name, value)
        for class_fqn, attr_name, value in self.class_patches:
            value = pickle.load(value)
            cls = locate(class_fqn)
            setattr(cls, attr_name, value)

    @staticmethod
    def create():
        module_patches = []
        class_patches = []
        for target, name in MONKEYPATCHES:
            if isinstance(target, ModuleType):
                new_val = getattr(target, name)
                module_name = target.__name__
                module_patches.append((module_name, name, new_val))
            elif isinstance(target, type):
                new_val = getattr(target, name)
                class_fqn = f"{target.__module__}.{target.__name__}"
                class_patches.append((class_fqn, name, new_val))

        return TestPatches(module_patches, class_patches)
