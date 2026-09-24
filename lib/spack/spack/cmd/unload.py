# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
import os
import sys

import spack.build_environment
import spack.cmd
import spack.cmd.common
import spack.user_environment as uenv
from spack.cmd.common import arguments

description = "remove package from the user environment"
section = "user environment"
level = "short"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    """Parser is only constructed so that this prints a nice help
    message with -h."""
    arguments.add_common_arguments(subparser, ["installed_specs"])

    shells = subparser.add_mutually_exclusive_group()
    shells.add_argument(
        "--sh",
        action="store_const",
        dest="shell",
        const="sh",
        help="print sh commands to activate the environment",
    )
    shells.add_argument(
        "--csh",
        action="store_const",
        dest="shell",
        const="csh",
        help="print csh commands to activate the environment",
    )
    shells.add_argument(
        "--fish",
        action="store_const",
        dest="shell",
        const="fish",
        help="print fish commands to load the package",
    )
    shells.add_argument(
        "--bat",
        action="store_const",
        dest="shell",
        const="bat",
        help="print bat commands to load the package",
    )
    shells.add_argument(
        "--pwsh",
        action="store_const",
        dest="shell",
        const="pwsh",
        help="print pwsh commands to load the package",
    )

    subparser.add_argument(
        "-a", "--all", action="store_true", help="unload all loaded Spack packages"
    )


def unload(parser, args, ctx):
    """unload spack packages from the user environment"""
    if args.specs and args.all:
        args.subparser.error(
            "cannot specify specs on command line when unloading all specs with '--all'"
        )

    hashes = os.environ.get(uenv.spack_loaded_hashes_var, "").split(os.pathsep)
    if args.specs:
        specs = [
            spack.cmd.disambiguate_spec_from_hashes(spec, hashes, store=ctx.store)
            for spec in spack.cmd.parse_specs(args.specs, ctx)
        ]
    else:
        specs = ctx.store.db.query(hashes=hashes)

    if not args.shell:
        specs_str = " ".join(args.specs) or "SPECS"

        spack.cmd.common.shell_init_instructions(
            "spack unload", "    eval `spack unload {sh_arg}` %s" % specs_str
        )
        return 1

    env_mod = spack.build_environment.modifications_for_specs(*specs, ctx=ctx).reversed()
    for spec in specs:
        env_mod.remove_path(uenv.spack_loaded_hashes_var, spec.dag_hash())
    cmds = env_mod.shell_modifications(args.shell)

    sys.stdout.write(cmds)
