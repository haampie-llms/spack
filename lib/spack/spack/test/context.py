# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import spack.config
import spack.context
import spack.test.utilities


def test_process_context_is_one_instance(config):
    ctx = spack.context.default()
    assert spack.context.default() is ctx
    assert ctx.network is spack.context.default().network


def test_set_default_replaces_the_process_context(config):
    previous = spack.context.default()
    ctx = spack.context.SpackContext(spack.config.create_from())
    assert spack.context.set_default(ctx) is previous
    try:
        assert spack.context.default() is ctx
    finally:
        spack.context.set_default(previous)
    assert spack.context.default() is previous


def test_use_configuration_swaps_the_process_context(config):
    previous = spack.context.default()
    with spack.test.utilities.use_configuration() as cfg:
        assert spack.context.default().config is cfg
    assert spack.context.default() is previous
