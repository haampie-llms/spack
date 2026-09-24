# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack.context import SpackContext
from spack.main import print_setup_info


def test_print_shell_vars_sh(capfd, ctx: SpackContext):
    print_setup_info(ctx.config, "sh")
    out, _ = capfd.readouterr()

    assert "_sp_sys_type=" in out
    assert "_sp_tcl_roots=" in out
    assert "_sp_lmod_roots=" in out
    assert "_sp_module_prefix" not in out


def test_print_shell_vars_csh(capfd, ctx: SpackContext):
    print_setup_info(ctx.config, "csh")
    out, _ = capfd.readouterr()

    assert "set _sp_sys_type = " in out
    assert "set _sp_tcl_roots = " in out
    assert "set _sp_lmod_roots = " in out
    assert "set _sp_module_prefix = " not in out
