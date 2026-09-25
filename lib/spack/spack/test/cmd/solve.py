# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import pytest

from spack.test.harness import SpackCommand

solve = SpackCommand("solve")

pytestmark = pytest.mark.usefixtures("mutable_config", "mock_packages")


def test_solve_output_reads_packages():
    """Tests the solutions of spack solve with outputs that read the packages."""
    assert "libelf" in solve("--non-defaults", "libelf")
    assert "libelf" in solve("--format", "{package.name}", "libelf")
