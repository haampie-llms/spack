# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Bootstrap the development dependencies of Spack in the bootstrapping environment."""

import os
import pathlib
import sys

import spack.config
import spack.environment
import spack.error
import spack.paths
import spack.spec
import spack.stage
import spack.store
import spack.util.gpg
from spack.util import tty

from .config import (
    DEV_GROUP,
    bootstrap_environment,
    development_requirements,
    ensure_bootstrap_configuration,
    root_path,
)
from .core import _add_externals_if_missing, ensure_clingo_importable_or_raise


def _installed(env: spack.environment.Environment) -> bool:
    """Return whether the development dependencies of Spack are concretized and installed"""
    concrete = dict(env.concretized_specs_by(group=DEV_GROUP))
    with spack.store.STORE.db.read_transaction():
        return all(
            s in concrete and concrete[s].installed for s in env.user_specs_by(group=DEV_GROUP)
        )


def _install(env: spack.environment.Environment) -> None:
    """Concretize and install the development dependencies of Spack, from signed binaries."""
    log_enabled = tty.is_debug() or tty.is_verbose()
    with tty.SuppressOutput(msg_enabled=log_enabled, warn_enabled=log_enabled):
        with env.write_transaction():
            env.concretize()
            env.write(regenerate=False)
    if _installed(env):
        return

    colorized_specs = [
        spack.spec.Spec(x).cformat("{name}{@version}") for x in development_requirements()
    ]
    tty.msg(f"[BOOTSTRAPPING] Installing dependencies ({', '.join(colorized_specs)})")
    gpg_home = os.path.join(root_path(), ".bootstrap_gpg_home")
    enable_source = spack.config.CONFIG.get("bootstrap:dev:enable_source", False)
    policy = "auto" if enable_source else "cache_only"
    # The mirrors of the development dependencies are in the configuration of their group
    with tty.SuppressOutput(
        msg_enabled=log_enabled, warn_enabled=log_enabled
    ), spack.util.gpg.gnupghome_override(gpg_home), env.config_override_for_group(group=DEV_GROUP):
        download_and_trust_key()
        roots = [root for _, root in env.concretized_specs_by(group=DEV_GROUP)]
        try:
            env.install_specs(
                roots, fail_fast=True, root_policy=policy, dependencies_policy=policy
            )
        except BaseException:
            # Concretize again next time, e.g. when binaries were missing
            with env.write_transaction():
                for user_spec, _ in list(env.concretized_specs_by(group=DEV_GROUP)):
                    env.deconcretize_by_user_spec(user_spec, group=DEV_GROUP)
                env.write(regenerate=False)
            raise
    with env.write_transaction():
        env.write(regenerate=True)


def load(env: spack.environment.Environment) -> None:
    """Make the development dependencies of Spack in the view of the environment available, by
    updating PATH and sys.path."""
    roots = [root for _, root in env.concretized_specs_by(group=DEV_GROUP)]
    if not roots:
        return
    venv = roots[0]["python-venv"].package
    view = pathlib.Path(env.default_view.root)
    # Make executables available (shouldn't need PYTHONPATH)
    os.environ["PATH"] = f"{view / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    # Spack itself imports pytest
    sys.path.extend({str(view / p) for p in (venv.platlib, venv.purelib)})


def download_and_trust_key():
    """Fetches and verifies the validity of Spack's public key"""
    fingerprint_file = (
        pathlib.Path(spack.paths.share_path) / "bootstrap" / "fingerprints" / "public.txt"
    )
    with open(fingerprint_file, "r", encoding="utf-8") as f:
        fingerprint, key_endpoint = f.readline().strip("\n").split(";")
    fingerprint = fingerprint.strip().upper()
    with spack.stage.stage_from_config(key_endpoint, config=spack.config.CONFIG) as stage:
        try:
            stage.fetch()
        except spack.error.FetchError as e:
            raise RuntimeError("Cannot fetch Spack Public key for binary cache validation") from e
        spack.util.gpg.trust(stage.save_filename, fprs=[fingerprint])


def ensure_environment_dependencies() -> None:
    """Ensure Spack dependencies from the bootstrap environment are installed and ready to use"""
    with ensure_bootstrap_configuration():
        env = bootstrap_environment()
        if not _installed(env):
            _add_externals_if_missing()
            # clingo is installed in the same environment, so it must be there before concretizing
            ensure_clingo_importable_or_raise()
            _install(env)
        load(env)
