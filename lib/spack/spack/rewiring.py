# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import tempfile
from typing import TYPE_CHECKING

import spack.binary_distribution
import spack.error
import spack.hooks
import spack.relocate

if TYPE_CHECKING:
    import spack.context


def rewire(spliced_spec, ctx: "spack.context.SpackContext"):
    """Given a spliced spec, this function conducts all the rewiring on all
    nodes in the DAG of that spec, in the store of ``ctx``."""
    store = ctx.store
    assert spliced_spec.spliced
    for spec in spliced_spec.traverse(order="post", root=True):
        if not store.db.installed(spec.build_spec):
            # TODO: May want to change this at least for the root spec...
            # TODO: Also remember to import PackageInstaller
            # PackageInstaller([spec.build_spec.package]).install()
            raise PackageNotInstalledError(spliced_spec, spec.build_spec, spec)
        if spec.build_spec is not spec and not store.db.installed(spec):
            explicit = spec is spliced_spec
            rewire_node(spec, explicit, ctx)


def rewire_node(spec, explicit, ctx: "spack.context.SpackContext"):
    """This function rewires a single node, worrying only about references to
    its subgraph. Binaries, text, and links are all changed in accordance with
    the splice. The resulting package is then 'installed.'"""
    tempdir = tempfile.mkdtemp()

    # Copy spec.build_spec.prefix to spec.prefix through a temporary tarball
    tarball = os.path.join(tempdir, f"{spec.dag_hash()}.tar.gz")
    store = ctx.store
    spack.binary_distribution.create_tarball(spec.build_spec, tarball, store=store)

    spack.hooks.pre_install(spec)
    spack.binary_distribution.extract_buildcache_tarball(tarball, destination=spec.prefix)
    spack.binary_distribution.relocate_package(
        spec, store=store, patchelf=spack.relocate.patchelf_finder(ctx)
    )

    # run post install hooks and add to db
    spack.hooks.post_install(spec, explicit)
    store.db.add(spec, explicit=explicit)


class RewireError(spack.error.SpackError):
    """Raised when something goes wrong with rewiring."""

    def __init__(self, message, long_msg=None):
        super().__init__(message, long_msg)


class PackageNotInstalledError(RewireError):
    """Raised when the build_spec for a splice was not installed."""

    def __init__(self, spliced_spec, build_spec, dep):
        super().__init__(
            """Rewire of {0}
            failed due to missing install of build spec {1}
            for spec {2}""".format(spliced_spec, build_spec, dep)
        )
