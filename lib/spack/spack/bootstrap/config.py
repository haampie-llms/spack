# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Manage configuration swapping for bootstrapping purposes"""

import contextlib
import hashlib
import os
import pathlib
import sys
from typing import Any, Dict, Generator, MutableSequence, Optional, Sequence

import spack.vendor.archspec.cpu

import spack.config
import spack.environment
import spack.paths
import spack.platforms
import spack.repo
import spack.store
import spack.tengine
from spack.error import ExplicitDatabaseUpgradeError
from spack.util import tty

#: Name of the configuration scope that is present only in the bootstrapping configuration
_SCOPE_NAME = "bootstrap"


def is_bootstrapping() -> bool:
    """Return True if we are in a bootstrapping context, False otherwise.

    The answer is read from the configuration, which installer child processes inherit.
    """
    return _SCOPE_NAME in spack.config.CONFIG.scopes


def spec_for_current_python() -> str:
    """For bootstrapping purposes we are just interested in the Python
    minor version (all patches are ABI compatible with the same minor).

    See:

    * https://www.python.org/dev/peps/pep-0513/
    * https://stackoverflow.com/a/35801395/771663
    """
    version_str = ".".join(str(x) for x in sys.version_info[:2])
    return f"python@{version_str}"


def environment_path(prefix: str = "") -> str:
    """Return the path of a bootstrapping environment for the interpreter running Spack.

    Args:
        prefix: prefix of the directory name, which otherwise identifies the interpreter
    """
    python_part = spec_for_current_python().replace("@", "")
    arch_part = spack.vendor.archspec.cpu.host().family
    interpreter_part = hashlib.md5(sys.exec_prefix.encode()).hexdigest()[:5]
    dirname = f"{prefix}{python_part}-{arch_part}-{interpreter_part}"
    return os.path.join(root_path(), "environments", dirname)


def root_path() -> str:
    """Root of all the bootstrap related folders"""
    return spack.config.canonicalize_path(
        spack.config.CONFIG.get("bootstrap:root", spack.paths.default_user_bootstrap_path)
    )


def store_path() -> str:
    """Path to the store used for bootstrapped software"""
    enabled = spack.config.CONFIG.get("bootstrap:enable", True)
    if not enabled:
        msg = 'bootstrapping is currently disabled. Use "spack bootstrap enable" to enable it'
        raise RuntimeError(msg)

    return _store_path()


def write_environment_manifest(
    path: str,
    *,
    specs: Sequence[str] = (),
    view: Optional[str] = None,
    mirrors: Sequence[str] = (),
) -> None:
    """Write the manifest of a bootstrapping environment, which installs into the bootstrap store
    and uses the interpreter running Spack as the only Python.

    Args:
        path: root directory of the environment
        specs: root specs of the environment
        view: path of the view, if any
        mirrors: names of the developer tools mirrors to use, which also select the concretizer
            settings for them
    """
    template = spack.tengine.make_environment().get_template("bootstrap/spack.yaml")
    context = {
        "python_spec": f"{spec_for_current_python()}+ctypes",
        "python_prefix": pathlib.Path(sys.exec_prefix).as_posix(),
        "architecture": spack.vendor.archspec.cpu.host().family,
        "view": pathlib.Path(view).as_posix() if view else "false",
        "environment_specs": specs,
        "store_path": pathlib.Path(store_path()).as_posix(),
        "bootstrap_mirrors": mirrors,
    }
    os.makedirs(path, exist_ok=True)
    pathlib.Path(path, spack.environment.manifest_name).write_text(
        template.render(context), encoding="utf-8"
    )


def core_environment() -> spack.environment.Environment:
    """Return the environment of the core dependencies of Spack, for the interpreter running it"""
    path = environment_path(prefix="core-")
    if not spack.environment.is_env_dir(path):
        write_environment_manifest(path)
    return spack.environment.Environment(path)


def _store_path() -> str:
    bootstrap_root_path = root_path()
    return spack.config.canonicalize_path(os.path.join(bootstrap_root_path, "store"))


def _config_path() -> str:
    bootstrap_root_path = root_path()
    return spack.config.canonicalize_path(os.path.join(bootstrap_root_path, "config"))


@contextlib.contextmanager
def ensure_bootstrap_configuration() -> Generator:
    """Swap the current configuration for the one used to bootstrap Spack.

    Nested use does not swap again. One compelling use case is bootstrapping patchelf during
    the bootstrap of clingo.
    """
    try:
        if is_bootstrapping():
            yield
        else:
            with _ensure_bootstrap_configuration():
                yield
    except ExplicitDatabaseUpgradeError as e:
        # Adjust database upgrade error messages to include -b flag when in bootstrap mode
        if e._long_message:
            e._long_message = e._long_message.replace("spack reindex", "spack -b reindex")
        raise


def _read_and_sanitize_configuration() -> Dict[str, Any]:
    """Read the user configuration that needs to be reused for bootstrapping
    and remove the entries that should not be copied over.
    """
    # Read the "config" section but pop the install tree (the entry will not be
    # considered due to the use_store context manager, so it will be confusing
    # to have it in the configuration).
    config_yaml = spack.config.CONFIG.get("config")
    config_yaml.pop("install_tree", None)
    return {
        "bootstrap": spack.config.CONFIG.get("bootstrap"),
        "config": config_yaml,
        "repos": spack.config.CONFIG.get("repos"),
    }


def _bootstrap_config_scopes() -> Sequence["spack.config.ConfigScope"]:
    tty.debug("[BOOTSTRAP CONFIG SCOPE] name=_builtin")
    config_scopes: MutableSequence["spack.config.ConfigScope"] = [
        spack.config.InternalConfigScope("_builtin", spack.config.CONFIG_DEFAULTS)
    ]
    configuration_paths = (spack.config.CONFIGURATION_DEFAULTS_PATH, (_SCOPE_NAME, _config_path()))
    for name, path in configuration_paths:
        generic_scope = spack.config.DirectoryConfigScope(name, path)
        config_scopes.append(generic_scope)
        msg = "[BOOTSTRAP CONFIG SCOPE] name={0}, path={1}"
        tty.debug(msg.format(generic_scope.name, generic_scope.path))
    return config_scopes


@contextlib.contextmanager
def _ensure_bootstrap_configuration() -> Generator:
    spack.repo.PATH.repos  # ensure this is instantiated from current config.
    spack.store.ensure_singleton_created()
    store_path()  # raises if bootstrapping is disabled
    user_configuration = _read_and_sanitize_configuration()
    with spack.environment.no_active_environment(), spack.platforms.use_platform(
        spack.platforms.real_host()
    ), spack.config.use_configuration(
        # Default configuration scopes excluding command line and builtin
        *_bootstrap_config_scopes()
    ):
        spack.config.CONFIG.set("bootstrap", user_configuration["bootstrap"])
        spack.config.CONFIG.set("config", user_configuration["config"])
        spack.config.CONFIG.set("repos", user_configuration["repos"])
        # The environment sets the store, the Python interpreter and the policies
        with core_environment():
            yield
