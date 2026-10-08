# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import pytest

from spack.main import SpackCommand

cd = SpackCommand("cd")

pytestmark = pytest.mark.usefixtures("config")


def test_cd():
    """Sanity check the cd command to make sure it works."""

    out = cd()

    assert "To set up shell support" in out
