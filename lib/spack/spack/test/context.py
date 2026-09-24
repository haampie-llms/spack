# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import pickle

import spack.config
import spack.context


def test_process_context_is_one_instance(config):
    ctx = spack.context.default()
    assert spack.context.default() is ctx
    assert ctx.network is spack.context.default().network


def test_process_context_members_follow_the_process_config(config):
    network = spack.context.default().network
    with spack.config.use_configuration():
        assert spack.context.default().network is not network
    assert spack.context.default().network is not network


def test_process_context_unpickles_to_the_process_context(config):
    ctx = spack.context.default()
    assert pickle.loads(pickle.dumps(ctx)) is ctx
