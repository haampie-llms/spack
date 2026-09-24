# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Bootstrap non-core Spack dependencies from an environment."""

import hashlib
import os
import pathlib
import shutil
import sys
from typing import TYPE_CHECKING, Iterable, List

import spack.vendor.archspec.cpu

import spack.config
import spack.environment
import spack.error
import spack.paths
import spack.repo
import spack.spec
import spack.stage
import spack.tengine
import spack.util.gpg
from spack.util import tty

from .config import root_path, spec_for_current_python, store_path
from .core import _add_externals_if_missing

if TYPE_CHECKING:
    import spack.context


class BootstrapEnvironment(spack.environment.Environment):
    """Environment to install dependencies of Spack for a given interpreter and architecture"""

    def __init__(self, ctx: "spack.context.SpackContext") -> None:
        """
        Args:
            ctx: bootstrap context the environment is read and installed in
        """
        root = self.environment_root(ctx.config)
        if not (root / "spack.yaml").exists():
            self._write_spack_yaml_file(root, ctx.config)
        super().__init__(root, ctx=ctx)

        # Remove python package roots created before python-venv was introduced
        for s in self.concrete_roots():
            if "python" in s.package.extendees and not s.dependencies("python-venv"):
                self.deconcretize_by_hash(s.dag_hash())

    @classmethod
    def spack_dev_requirements(cls) -> List[str]:
        """Spack development requirements"""
        return [pytest_root_spec(), ruff_root_spec(), mypy_root_spec()]

    @classmethod
    def environment_root(cls, config: spack.config.Configuration) -> pathlib.Path:
        """Environment root directory"""
        bootstrap_root_path = root_path(config)
        python_part = spec_for_current_python().replace("@", "")
        arch_part = spack.vendor.archspec.cpu.host().family
        interpreter_part = hashlib.md5(sys.exec_prefix.encode()).hexdigest()[:5]
        environment_dir = f"{python_part}-{arch_part}-{interpreter_part}"
        return pathlib.Path(
            spack.config.canonicalize_path(
                os.path.join(bootstrap_root_path, "environments", environment_dir), config=config
            )
        )

    @classmethod
    def bootstrap_gpg_home(cls, config: spack.config.Configuration) -> pathlib.Path:
        """Location of the GPG home directory used for bootstrapping"""
        return pathlib.Path(root_path(config)).joinpath(".bootstrap_gpg_home")

    def view_root(self) -> pathlib.Path:
        """Location of the view"""
        return pathlib.Path(self.path).joinpath("view")

    def bin_dir(self) -> pathlib.Path:
        """Paths to be added to PATH"""
        return self.view_root().joinpath("bin")

    def python_dirs(self) -> Iterable[pathlib.Path]:
        venv = next(s for s in self.all_specs_generator() if s.name == "python-venv")
        spack.repo.attach_packages([venv], self.ctx)
        python = venv.package
        return {self.view_root().joinpath(p) for p in (python.platlib, python.purelib)}

    def update_installations(self) -> None:
        """Update the installations of this environment."""
        log_enabled = tty.is_debug() or tty.is_verbose()
        with tty.SuppressOutput(msg_enabled=log_enabled, warn_enabled=log_enabled):
            specs = self.concretize()
        if specs:
            colorized_specs = [
                spack.spec.Spec(x).cformat("{name}{@version}")
                for x in self.spack_dev_requirements()
            ]
            tty.msg(f"[BOOTSTRAPPING] Installing dependencies ({', '.join(colorized_specs)})")
            self.write(regenerate=False)
            with tty.SuppressOutput(msg_enabled=log_enabled, warn_enabled=log_enabled):
                gnupghome = str(self.bootstrap_gpg_home(self.ctx.config))
                with spack.util.gpg.gnupghome_override(gnupghome):
                    download_and_trust_key(self.ctx)
                    fetch_policy = (
                        "cache_only"
                        if not self.ctx.config.get("bootstrap:dev:enable_source", False)
                        else "auto"
                    )
                    try:
                        self.install_all(
                            fail_fast=True,
                            root_policy=fetch_policy,
                            dependencies_policy=fetch_policy,
                        )
                    except BaseException:
                        # catch any exception as we always want to clean up
                        shutil.rmtree(self.path)
                        raise
                    self.write(regenerate=True)

    def load(self) -> None:
        """Update PATH and sys.path."""
        # Make executables available (shouldn't need PYTHONPATH)
        os.environ["PATH"] = f"{self.bin_dir()}{os.pathsep}{os.environ.get('PATH', '')}"

        # Spack itself imports pytest
        sys.path.extend(str(p) for p in self.python_dirs())

    @classmethod
    def _write_spack_yaml_file(
        cls, root: pathlib.Path, config: spack.config.Configuration
    ) -> None:
        tty.msg(
            "[BOOTSTRAPPING] Spack has missing dependencies, creating a bootstrapping environment"
        )
        env = spack.tengine.make_environment(config)
        template = env.get_template("bootstrap/spack.yaml")
        context = {
            "python_spec": f"{spec_for_current_python()}+ctypes",
            "python_prefix": pathlib.Path(sys.exec_prefix).as_posix(),
            "architecture": spack.vendor.archspec.cpu.host().family,
            "environment_path": root.as_posix(),
            "environment_specs": cls.spack_dev_requirements(),
            "store_path": pathlib.Path(store_path(config)).as_posix(),
            "bootstrap_mirrors": dev_bootstrap_mirror_names(),
        }
        root.mkdir(parents=True, exist_ok=True)
        (root / "spack.yaml").write_text(template.render(context), encoding="utf-8")


def mypy_root_spec() -> str:
    """Return the root spec used to bootstrap mypy"""
    return "py-mypy@0.900: ^py-mypy-extensions@:1.0"


def pytest_root_spec() -> str:
    """Return the root spec used to bootstrap pytest"""
    return "py-pytest@6.2.4:"


def ruff_root_spec() -> str:
    """Return the root spec used to bootstrap ruff"""
    return "py-ruff@0.15.0"


def dev_bootstrap_mirror_names() -> List[str]:
    """Return the mirror names used for bootstrapping dev
    requirements"""
    return [
        "developer-tools-darwin",
        "developer-tools-x86_64_v3-linux-gnu",
        "developer-tools-aarch64-linux-gnu",
    ]


def download_and_trust_key(ctx: "spack.context.SpackContext"):
    """Fetches and verifies the validity of Spack's public key"""
    fingerprint_file = (
        pathlib.Path(spack.paths.share_path) / "bootstrap" / "fingerprints" / "public.txt"
    )
    with open(fingerprint_file, "r", encoding="utf-8") as f:
        fingerprint, key_endpoint = f.readline().strip("\n").split(";")
    fingerprint = fingerprint.strip().upper()
    with spack.stage.stage_from_config(
        key_endpoint, config=ctx.config, client=ctx.network
    ) as stage:
        try:
            stage.fetch()
        except spack.error.FetchError as e:
            raise RuntimeError("Cannot fetch Spack Public key for binary cache validation") from e
        spack.util.gpg.trust(stage.save_filename, fprs=[fingerprint])


def ensure_environment_dependencies(ctx: "spack.context.SpackContext") -> None:
    """Ensure Spack dependencies from the bootstrap environment are installed and ready to use"""
    bootstrap = ctx.bootstrap
    _add_externals_if_missing(bootstrap)
    with BootstrapEnvironment(bootstrap) as env:
        env.update_installations()
        env.load()
