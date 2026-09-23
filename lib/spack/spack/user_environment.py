# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import os
import re
import sys

import spack.build_environment
import spack.config
import spack.context
import spack.spec
from spack import traverse
from spack.enums import Context
from spack.util import environment

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


def modifications_for_specs(
    *specs: spack.spec.Spec,
    config: spack.config.Configuration,
    view=None,
    set_package_py_globals: bool = True,
):
    """List of environment (shell) modifications to be processed for spec.

    This list is specific to the location of the spec or its projection in
    the view.

    Args:
        specs: spec(s) for which to list the environment modifications
        config: configuration to read the prefix inspections from
        view: view associated with the spec passed as first argument
        set_package_py_globals: whether or not to set the global variables in the
            package.py files (this may be problematic when using buildcaches that have
            been built on a different but compatible OS)
    """
    env = environment.EnvironmentModifications()
    topo_ordered = list(
        traverse.traverse_nodes(specs, root=True, deptype=("run", "link"), order="topo")
    )

    # Static environment changes (prefix inspections)
    for s in reversed(topo_ordered):
        static = environment.inspect_path(
            s.prefix, prefix_inspections(s.platform, config), exclude=environment.is_system_path
        )
        env.extend(static)

    # Dynamic environment changes (setup_run_environment etc)
    setup_context = spack.build_environment.SetupContext(*specs, context=Context.RUN)
    if set_package_py_globals:
        setup_context.set_all_package_py_globals()
    env.extend(setup_context.get_env_modifications())

    # Apply view projections if any.
    if view:
        project_env_mods(*topo_ordered, view=view, env=env, config=config)

    return env


def environment_modifications_for_specs(
    *specs: spack.spec.Spec, view=None, set_package_py_globals: bool = True
):
    """Same as :func:`modifications_for_specs`, with the current configuration.

    This is part of the package API; library code calls :func:`modifications_for_specs`.
    """
    return modifications_for_specs(
        *specs,
        config=spack.context.current().config,
        view=view,
        set_package_py_globals=set_package_py_globals,
    )
