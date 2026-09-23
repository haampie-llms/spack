# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
import sys

import spack.context
import spack.repo
from spack import cmd
from spack.cmd.common import arguments
from spack.util import tty
from spack.util.tty.colify import colify

description = "list extensions for package"
section = "query"
level = "long"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.epilog = (
        "If called without argument returns the list of all valid extendable packages"
    )
    arguments.add_common_arguments(subparser, ["long", "very_long"])
    subparser.add_argument(
        "-d", "--deps", action="store_true", help="output dependencies along with found specs"
    )

    subparser.add_argument(
        "-p", "--paths", action="store_true", help="show paths to package install directories"
    )
    subparser.add_argument(
        "-s",
        "--show",
        action="store",
        default="all",
        choices=("packages", "installed", "all"),
        help="show only part of output",
    )

    subparser.add_argument(
        "spec",
        nargs=argparse.REMAINDER,
        help="spec of package to list extensions for",
        metavar="extendable",
    )


def extensions(parser, args, ctx: spack.context.SpackContext):
    if not args.spec:
        # If called without arguments, list all the extendable packages
        isatty = sys.stdout.isatty()
        if isatty:
            tty.info("Extendable packages:")

        extendable_pkgs = []
        for name in ctx.repo.all_package_names():
            pkg_cls = ctx.repo.get_pkg_class(name)
            if pkg_cls.extendable:
                extendable_pkgs.append(name)

        colify(extendable_pkgs, indent=4)
        return

    # Checks
    spec = cmd.parse_specs(args.spec, ctx)
    if len(spec) > 1:
        args.subparser.error("can only list extensions for one package")

    spec = cmd.disambiguate_spec(spec[0], ctx.environment, store=ctx.store)
    spack.repo.attach_packages([spec], ctx)

    if not spec.package.extendable:
        tty.die("%s is not an extendable package." % spec.name)

    if not spec.package.extendable:
        tty.die("%s does not have extensions." % spec.short_spec)

    if args.show in ("packages", "all"):
        # List package names of extensions
        extensions = ctx.repo.extensions_for(spec)
        if not extensions:
            tty.msg("%s has no extensions." % spec.cshort_spec)
        else:
            tty.msg(spec.cshort_spec)
            tty.msg("%d extensions:" % len(extensions))
            colify([ext.name for ext in extensions])

    if args.show in ("installed", "all"):
        # List specs of installed extensions.
        candidates = ctx.store.db.query()
        spack.repo.attach_packages(candidates, ctx)
        installed = [s for s in candidates if s.has_package and s.package.extends(spec)]

        if args.show == "all":
            print
        if not installed:
            tty.msg("None installed.")
        else:
            tty.msg("%d installed:" % len(installed))
            with ctx.store.db.read_transaction():
                cmd.display_specs(installed, args)
