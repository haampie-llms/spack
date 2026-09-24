# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import spack.config
import spack.context


def test_set_default_replaces_the_process_context(ctx):
    previous = spack.context.default()
    other = spack.context.SpackContext(spack.config.create_from())
    assert spack.context.set_default(other) is previous
    try:
        assert spack.context.default() is other
    finally:
        spack.context.set_default(previous)
    assert spack.context.default() is previous


def test_the_test_context_is_the_process_context(ctx):
    """Package API functions read the context of the process outside package code"""
    assert spack.context.default() is ctx
