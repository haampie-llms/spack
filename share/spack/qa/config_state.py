# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Used to test correct application of config line scopes in various cases.

The option `config:cache` is supposed to be False, and overridden to True
from the command line. Run with `spack python`, which provides the context as `ctx`.
"""

import multiprocessing as mp


def show_config(ctx):
    result = ctx.config.get("config:ccache")
    if result is not True:
        raise RuntimeError(f"Expected config:ccache:true, but got {result}")


if __name__ == "__main__":
    for method in ("spawn", "fork"):
        print(f"Testing {method}")
        p = mp.get_context(method).Process(target=show_config, args=(ctx,))  # noqa: F821
        p.start()
        p.join()
        if p.exitcode != 0:
            raise SystemExit(p.exitcode)
