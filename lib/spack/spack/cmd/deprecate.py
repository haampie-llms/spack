# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Deprecate one Spack install in favor of another

Spack packages of different configurations cannot be installed to the same
location. However, in some circumstances (e.g. security patches) old
installations should never be used again. In these cases, we will mark the old
installation as deprecated, remove it, and link another installation into its
place.

It is up to the user to ensure binary compatibility between the deprecated
installation and its deprecator.
"""

import argparse
import os
import shutil

import spack.cmd
import spack.concretize
import spack.installer_dispatch
import spack.package_base
import spack.spec
import spack.store
import spack.util.filesystem as fs
from spack.cmd.common import arguments
from spack.util import tty
from spack.util.filesystem import symlink

from ..enums import InstallRecordStatus

description = "replace one package with another via symlinks"
section = "admin"
level = "long"

# Arguments for display_specs when we find ambiguity
display_args = {"long": True, "show_flags": True, "variants": True, "indent": 4}


def setup_parser(sp: argparse.ArgumentParser) -> None:
    setattr(setup_parser, "parser", sp)

    arguments.add_common_arguments(sp, ["yes_to_all"])

    deps = sp.add_mutually_exclusive_group()
    deps.add_argument(
        "-d",
        "--dependencies",
        action="store_true",
        default=True,
        dest="dependencies",
        help="deprecate dependencies (default)",
    )
    deps.add_argument(
        "-D",
        "--no-dependencies",
        action="store_false",
        default=True,
        dest="dependencies",
        help="do not deprecate dependencies",
    )

    install = sp.add_mutually_exclusive_group()
    install.add_argument(
        "-i",
        "--install-deprecator",
        action="store_true",
        default=False,
        dest="install",
        help="concretize and install deprecator spec",
    )
    install.add_argument(
        "-I",
        "--no-install-deprecator",
        action="store_false",
        default=False,
        dest="install",
        help="deprecator spec must already be installed (default)",
    )

    sp.add_argument(
        "specs", nargs=argparse.REMAINDER, help="spec to deprecate and spec to use as deprecator"
    )


def deprecate(parser, args, ctx):
    """Deprecate one spec in favor of another"""
    env = ctx.environment
    specs = spack.cmd.parse_specs(args.specs, ctx)

    if len(specs) != 2:
        args.subparser.error("requires exactly two specs")

    deprecate = spack.cmd.disambiguate_spec(
        specs[0],
        env,
        store=ctx.store,
        local=True,
        installed=(InstallRecordStatus.INSTALLED | InstallRecordStatus.DEPRECATED),
    )

    if args.install:
        deprecator = spack.concretize.concretize_one(specs[1], ctx)
    else:
        deprecator = spack.cmd.disambiguate_spec(specs[1], env, store=ctx.store, local=True)

    # calculate all deprecation pairs for errors and warning message
    all_deprecate = []
    all_deprecators = []

    generator = (
        deprecate.traverse(order="post", deptype="link", root=True)
        if args.dependencies
        else [deprecate]
    )
    for spec in generator:
        all_deprecate.append(spec)
        all_deprecators.append(deprecator[spec.name])
        # This will throw a key error if deprecator does not have a dep
        # that matches the name of a dep of the spec

    if not args.yes_to_all:
        tty.msg("The following packages will be deprecated:\n")
        spack.cmd.display_specs(all_deprecate, **display_args)
        tty.msg("In favor of (respectively):\n")
        spack.cmd.display_specs(all_deprecators, **display_args)
        print()

        already_deprecated = []
        already_deprecated_for = []
        for spec in all_deprecate:
            deprecated_for = ctx.store.db.deprecator(spec)
            if deprecated_for:
                already_deprecated.append(spec)
                already_deprecated_for.append(deprecated_for)

        tty.msg("The following packages are already deprecated:\n")
        spack.cmd.display_specs(already_deprecated, **display_args)
        tty.msg("In favor of (respectively):\n")
        spack.cmd.display_specs(already_deprecated_for, **display_args)

        answer = tty.get_yes_or_no("Do you want to proceed?", default=False)
        if not answer:
            tty.die("Will not deprecate any packages.")

    # Fail before touching the store if the database cannot be modified.
    ctx.store.db.ensure_latest_db_version()

    for dcate, dcator in zip(all_deprecate, all_deprecators):
        deprecate_spec(dcate, dcator, symlink, ctx.store)


def deprecate_spec(
    spec: spack.spec.Spec, deprecator: spack.spec.Spec, link_fn, store: spack.store.Store
) -> None:
    """Deprecate ``spec`` in favor of ``deprecator``"""
    # Here we assume we don't deprecate across different stores, and that same hash
    # means same binary artifacts
    if spec.dag_hash() == deprecator.dag_hash():
        return

    # We can't really have control over external specs, and cannot link anything in their place
    if spec.external:
        return

    # Install deprecator if it isn't installed already
    if not store.db.query(deprecator):
        spack.installer_dispatch.create_installer([deprecator.package], explicit=True).install()

    old_deprecator = store.db.deprecator(spec)
    if old_deprecator:
        # Find this spec file from its old deprecation
        specfile = store.layout.deprecated_file_path(spec, old_deprecator)
    else:
        specfile = store.layout.spec_file_path(spec)

    # copy spec metadata to "deprecated" dir of deprecator
    depr_specfile = store.layout.deprecated_file_path(spec, deprecator)
    fs.mkdirp(os.path.dirname(depr_specfile))
    shutil.copy2(specfile, depr_specfile)

    # Any specs deprecated in favor of this spec are re-deprecated in favor of its new deprecator
    for deprecated in store.db.specs_deprecated_by(spec):
        deprecate_spec(deprecated, deprecator, link_fn, store)

    # Now that we've handled metadata, uninstall and replace with link
    spack.package_base.PackageBase.uninstall_by_spec(
        spec, store, force=True, deprecator=deprecator
    )
    link_fn(deprecator.prefix, spec.prefix)
