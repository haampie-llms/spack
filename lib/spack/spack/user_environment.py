# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import os
import re
import sys
from typing import TYPE_CHECKING

import spack.config
import spack.spec
from spack.util import environment

if TYPE_CHECKING:
    import spack.context

#: Environment variable name Spack uses to track individually loaded packages
spack_loaded_hashes_var = "SPACK_LOADED_HASHES"


def prefix_inspections(platform: str, config: spack.config.Configuration) -> dict:
    """Get list of prefix inspections for platform

    Arguments:
        platform: the name of the platform to consider. The platform determines what environment
            variables Spack will use for some inspections.
        config: configuration to read ``modules:prefix_inspections`` from

    Returns:
        A dictionary mapping subdirectory names to lists of environment variables to modify with
        that directory if it exists.
    """
    inspections = config.get("modules:prefix_inspections")
    if isinstance(inspections, dict):
        return inspections

    inspections = {
        "bin": ["PATH"],
        "man": ["MANPATH"],
        "share/man": ["MANPATH"],
        "share/aclocal": ["ACLOCAL_PATH"],
        "lib/pkgconfig": ["PKG_CONFIG_PATH"],
        "lib64/pkgconfig": ["PKG_CONFIG_PATH"],
        "share/pkgconfig": ["PKG_CONFIG_PATH"],
        "": ["CMAKE_PREFIX_PATH"],
    }

    if platform == "darwin":
        inspections["lib"] = ["DYLD_FALLBACK_LIBRARY_PATH"]
        inspections["lib64"] = ["DYLD_FALLBACK_LIBRARY_PATH"]

    return inspections


def unconditional_environment_modifications(view, config: spack.config.Configuration):
    """List of environment (shell) modifications to be processed for view.

    This list does not depend on the specs in this environment"""
    env = environment.EnvironmentModifications()

    for subdir, vars in prefix_inspections(sys.platform, config).items():
        full_subdir = os.path.join(view.root, subdir)
        for var in vars:
            env.prepend_path(var, full_subdir)

    return env


def project_env_mods(
    *specs: spack.spec.Spec,
    view,
    env: environment.EnvironmentModifications,
    config: spack.config.Configuration,
) -> None:
    """Given a list of environment modifications, project paths changes to the view."""
    prefix_to_prefix = {
        str(s.prefix): view.get_projection_for_spec(s, config) for s in specs if not s.external
    }
    # Avoid empty regex if all external
    if not prefix_to_prefix:
        return
    prefix_regex = re.compile("|".join(re.escape(p) for p in prefix_to_prefix.keys()))
    for mod in env.env_modifications:
        if isinstance(mod, environment.NameValueModifier):
            mod.value = prefix_regex.sub(lambda m: prefix_to_prefix[m.group(0)], mod.value)
