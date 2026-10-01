# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""This package contains modules with hooks for various stages in the
Spack install process.  You can add modules here and they'll be
executed by package at various times during the package lifecycle.

Each hook is just a function that takes a package as a parameter.
Hooks are not executed in any particular order.

Currently the following hooks are supported:

* ``pre_install(spec)``
* ``post_install(spec, explicit)``: operates on the install prefix only
* ``post_install_publish(spec, explicit)``: publishes the installed package outside of the prefix
  (module files, build caches). Runs after all ``post_install`` hooks, outside the build sandbox.
* ``pre_uninstall(spec)``
* ``post_uninstall(spec)``

This can be used to implement support for things like module
systems (e.g. modules, lmod, etc.) or to add other custom
features.
"""

import importlib
import types
from typing import List, Optional


class _HookRunner:
    #: Order in which hooks are executed
    HOOK_ORDER = [
        "spack.hooks.module_file_generation",
        "spack.hooks.licensing",
        "spack.hooks.sbang",
        "spack.hooks.windows_runtime_linkage",
        "spack.hooks.drop_redundant_rpaths",
        "spack.hooks.absolutify_elf_sonames",
        "spack.hooks.permissions_setters",
        "spack.hooks.resolve_shared_libraries",
        # after all mutations to the install prefix, write metadata
        "spack.hooks.sbom_generate",
        "spack.hooks.write_install_manifest",
        # after all metadata is written
        "spack.hooks.autopush",
    ]

    #: Contains all hook modules after first call, shared among all HookRunner objects
    _hooks: Optional[List[types.ModuleType]] = None

    def __init__(self, hook_name):
        self.hook_name = hook_name

    @property
    def hooks(self) -> List[types.ModuleType]:
        if not self._hooks:
            self._hooks = [importlib.import_module(module_name) for module_name in self.HOOK_ORDER]
        return self._hooks

    def __call__(self, *args, **kwargs):
        for module in self.hooks:
            if hasattr(module, self.hook_name):
                hook = getattr(module, self.hook_name)
                if hasattr(hook, "__call__"):
                    hook(*args, **kwargs)


def load() -> None:
    """Import all hook modules, e.g. before forking processes that run hooks."""
    _HookRunner("load").hooks


# pre/post install and run by the install subprocess
pre_install = _HookRunner("pre_install")
post_install_prefix = _HookRunner("post_install")
post_install_publish = _HookRunner("post_install_publish")


def post_install(spec, explicit) -> None:
    """Run all post install hooks: first those that operate on the install prefix, then those
    that publish the installed package."""
    post_install_prefix(spec, explicit)
    post_install_publish(spec, explicit)


pre_uninstall = _HookRunner("pre_uninstall")
post_uninstall = _HookRunner("post_uninstall")
