# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""The conditions a base class declares are emitted once, and the ASP program copies them to
every package inheriting from it; see ``SpackSolverSetup.mixin_rules``."""

import pytest

import spack.concretize
import spack.context
import spack.solver.asp
from spack.spec import Spec

pytestmark = [pytest.mark.usefixtures("mutable_config", "mock_packages")]

BASE = "spack_repo.builtin_mock.packages.simple_inheritance.package.BaseWithDirectives"


def asp_problem(*specs: str):
    setup = spack.solver.asp.SpackSolverSetup(context=spack.context.default())
    return setup.setup([Spec(s) for s in specs], reuse=[], allow_deprecated=False).asp_problem


def test_base_class_conditions_are_emitted_once():
    problem = asp_problem("simple-inheritance", "multimodule-inheritance")

    # both packages declare that they inherit the conditions of the base class
    for pkg in ("simple-inheritance", "multimodule-inheritance"):
        assert f'pkg_fact("{pkg}",mixin("{BASE}")).' in problem

    # the dependencies of the base class are emitted once, in its name
    reasons = [line for line in problem if "depends on mpi" in line]
    assert len(reasons) == 1
    assert reasons[0].startswith("condition_reason(")
    assert '"BaseWithDirectives depends on mpi"' in reasons[0]
    assert any(x.startswith(f'mixin_fact("{BASE}",condition(') for x in problem)

    # the packages' own dependencies are emitted for them
    assert any('"simple-inheritance depends on openblas when +openblas"' in x for x in problem)

    # in shared clauses a placeholder stands for the inheriting package, on both sides
    assert any(
        x.startswith("mixin_condition_requirement(") and x.endswith('"node",mixin_self).')
        for x in problem
    )
    assert any(
        x.startswith("mixin_imposed_constraint(") and '"dependency_holds",mixin_self,"mpi",' in x
        for x in problem
    )


@pytest.mark.parametrize(
    "spec_str,expected,unexpected",
    [
        ("simple-inheritance", ["^mpi", "^cmake", "^openblas"], []),
        ("simple-inheritance~openblas", ["^mpi", "^cmake"], ["^openblas"]),
        ("multimodule-inheritance", ["^mpi", "^cmake"], []),
        ("cmake-client", ["^cmake"], []),
        ("py-extension1", ["^python"], []),
    ],
)
def test_inherited_directives_are_concretized(spec_str, expected, unexpected):
    s = spack.concretize.concretize_one(spec_str)
    assert all(s.satisfies(x) for x in expected)
    assert not any(s.satisfies(x) for x in unexpected)


def test_inherited_conflict_is_reported():
    """A conflict declared by a base class is reported in its name, with its message."""
    with pytest.raises(spack.solver.asp.UnsatisfiableSpecError) as exc:
        spack.concretize.concretize_one("simple-inheritance+openblas%clang")
    assert "BaseWithDirectives: openblas cannot be built with clang" in str(exc.value)

    s = spack.concretize.concretize_one("simple-inheritance~openblas%clang")
    assert s.satisfies("%clang")
