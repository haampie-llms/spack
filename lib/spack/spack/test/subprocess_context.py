# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import multiprocessing
import warnings

from spack.subprocess_context import GlobalStateMarshaler


def _warn(state: GlobalStateMarshaler) -> None:
    state.restore()
    warnings.warn("careful")


def test_child_processes_show_warnings_like_spack(capfd):
    """Tests that warnings in child processes, e.g. from post-install hooks of externals in
    build processes, are shown like other Spack warnings."""
    mp = multiprocessing.get_context("spawn")
    p = mp.Process(target=_warn, args=(GlobalStateMarshaler(ctx=mp),))
    p.start()
    p.join()
    err = capfd.readouterr().err
    assert "Warning: careful" in err and "UserWarning" not in err
