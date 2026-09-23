# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""The context Spack's own dependencies are bootstrapped in"""

import os
import sys
from typing import TYPE_CHECKING, List

import spack.config
import spack.paths
from spack.util import tty

if TYPE_CHECKING:
    import spack.context


def spec_for_current_python() -> str:
    """For bootstrapping purposes we are just interested in the Python
    minor version (all patches are ABI compatible with the same minor).

    See:

    * https://www.python.org/dev/peps/pep-0513/
    * https://stackoverflow.com/a/35801395/771663
    """
    version_str = ".".join(str(x) for x in sys.version_info[:2])
    return f"python@{version_str}"


def root_path(config: spack.config.Configuration) -> str:
    """Root of all the bootstrap related folders"""
    return spack.config.canonicalize_path(
        config.get("bootstrap:root", spack.paths.default_user_bootstrap_path), config=config
    )


def store_path(config: spack.config.Configuration) -> str:
    """Path to the store used for bootstrapped software"""
    enabled = config.get("bootstrap:enable", True)
    if not enabled:
        msg = 'bootstrapping is currently disabled. Use "spack bootstrap enable" to enable it'
        raise RuntimeError(msg)

    return spack.config.canonicalize_path(os.path.join(root_path(config), "store"), config=config)


def _config_path(config: spack.config.Configuration) -> str:
    return spack.config.canonicalize_path(os.path.join(root_path(config), "config"), config=config)


def _bootstrap_config_scopes(config: spack.config.Configuration) -> List[spack.config.ConfigScope]:
    tty.debug("[BOOTSTRAP CONFIG SCOPE] name=_builtin")
    config_scopes: List[spack.config.ConfigScope] = [
        spack.config.InternalConfigScope("_builtin", spack.config.CONFIG_DEFAULTS)
    ]
    configuration_paths = (
        spack.config.CONFIGURATION_DEFAULTS_PATH,
        ("bootstrap", _config_path(config)),
    )
    for name, path in configuration_paths:
        generic_scope = spack.config.DirectoryConfigScope(name, path)
        config_scopes.append(generic_scope)
        tty.debug(f"[BOOTSTRAP CONFIG SCOPE] name={generic_scope.name}, path={generic_scope.path}")
    return config_scopes


def bootstrap_context(ctx: "spack.context.SpackContext") -> "spack.context.SpackContext":
    """Return the context to bootstrap Spack's own dependencies in, derived from ``ctx``.

    Its configuration has the default and bootstrap scopes, plus the ``bootstrap``, ``config``
    and ``repos`` sections of ``ctx``. Software is installed in the bootstrap store, and the
    interpreter running Spack is the only Python. It shares the repositories and caches of
    ``ctx``.
    """
    user = ctx.config
    bootstrap_store = store_path(user)

    config = spack.config.Configuration()
    for scope in _bootstrap_config_scopes(user):
        config.push_scope(scope)

    # The user's install tree is replaced by the bootstrap store
    user_config = {k: v for k, v in user.get("config").items() if k != "install_tree"}
    user_data = {
        "bootstrap": user.get("bootstrap"),
        "config": user_config,
        "repos": user.get("repos"),
    }
    config.push_scope(spack.config.InternalConfigScope("bootstrap_user", user_data))

    python = {
        "buildable": False,
        "externals": [{"prefix": sys.exec_prefix, "spec": spec_for_current_python()}],
    }
    overrides = {
        "config": {"install_tree": {"root": bootstrap_store, "padded_length": 0}},
        "modules:": {"default": {"enable": []}},
        "packages": {"python:": python},
    }
    config.push_scope(spack.config.InternalConfigScope("bootstrap_overrides", overrides))

    result = type(ctx)(config, is_bootstrap=True)
    result.gpg_home = os.path.join(root_path(user), ".bootstrap_gpg_home")
    result.share(ctx, "repo", "misc_cache", "compiler_cache", "network")
    return result
