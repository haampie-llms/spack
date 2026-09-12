# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import re

import pytest

import spack.cmd.info
import spack.util.tty.color
from spack.main import SpackCommand, SpackCommandError
from spack.repo import UnknownPackageError

pytestmark = [pytest.mark.usefixtures("mock_packages")]

info = SpackCommand("info")


@pytest.fixture
def terminal(monkeypatch):
    """Pretend stdout is an 80 column terminal, to get the multi-line layout."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(spack.cmd.info, "_stdout_is_tty", lambda: True)


@pytest.fixture
def pipe(monkeypatch):
    """Pretend stdout is a pipe, to get the one-line-per-entry layout."""
    monkeypatch.setattr(spack.cmd.info, "_stdout_is_tty", lambda: False)


def test_package_suggestion():
    with pytest.raises(UnknownPackageError) as exc_info:
        info("vtk")
    assert "Did you mean one of the following packages?" in str(exc_info.value)


def test_deprecated_option_warns():
    info("--variants-by-name", "vtk-m")
    assert "--variants-by-name is deprecated" in info.output


# no specs, more than one spec
@pytest.mark.parametrize("args", [[], ["vtk-m", "zmpi"]])
def test_info_failures(args):
    with pytest.raises(SpackCommandError):
        info(*args)


def test_info_noversion():
    """Check that a mock package with no versions outputs None."""
    output = info("noversion")

    assert "Preferred version:\n    None" in output
    assert "Safe versions:\n    None" in output
    assert "Deprecated versions" not in output


@pytest.mark.parametrize(
    "pkg_query,expected", [("zlib", "False"), ("find-externals1", "True (version)")]
)
def test_is_externally_detectable(pkg_query, expected):
    output = info("--detectable", pkg_query)
    assert re.search(rf"Externally detectable:\s+{re.escape(expected)}\n", output)


@pytest.mark.parametrize(
    "pkg_query",
    ["vtk-m", "gcc"],  # This should ensure --test's c_names processing loop covered
)
@pytest.mark.parametrize("extra_args", [[], ["--by-when"]])
def test_info_fields(pkg_query, extra_args):
    expected_fields = (
        "Homepage:",
        "Externally detectable:",
        "Safe versions:",
        "Variants:",
        "Phases:",
        "Provides:",
        "Tags:",
        "Licenses:",
        "Stand-alone tests:",
    )

    output = info("--all", *extra_args, pkg_query)
    assert all(field in output for field in expected_fields)


def test_header_and_labels(pipe):
    output = info("--all", "mpich")
    assert output.startswith("mpich (Package)\n")
    # labels are aligned, values follow after a gutter
    assert re.search(r"^Homepage:\s+http://www.mpich.org\n", output, re.M)
    assert re.search(r"^Tags:\s+detectable, tag1, tag2\n", output, re.M)
    assert re.search(r"^Provides:\s+mpi@[\d.:]+ when @[\d.:]+\n", output, re.M)


@pytest.mark.parametrize(
    "args,in_output,not_in_output",
    [
        # no variants
        (["package-base-extendee"], [r"Variants:\n\s*None"], []),
        # conditional dependencies show up with their condition
        (
            ["long-boost-dependency+longdep"],
            [r"boost\+atomic\+chrono\+date_time\+filesystem\+graph\+iostreams\+locale"],
            [],
        ),
        (
            ["long-boost-dependency~longdep"],
            [],
            [r"boost\+atomic\+chrono\+date_time\+filesystem\+graph\+iostreams\+locale"],
        ),
        # conditional licenses change output
        (["licenses-1 +foo"], ["MIT"], ["Apache-2.0"]),
        (["licenses-1 ~foo"], ["Apache-2.0"], ["MIT"]),
        # filtering bowtie versions
        (["bowtie"], ["1.4.0", "1.3.0", "1.2.2", "1.2.0"], []),
        (["bowtie@1.2:"], ["1.4.0", "1.3.0", "1.2.2", "1.2.0"], []),
        (["bowtie@1.3:"], ["1.4.0", "1.3.0"], ["1.2.2", "1.2.0"]),
        (["bowtie@1.2"], [r"Preferred version:\n    1\.2\.2"], ["1.3.0", "1.4.0"]),
        # forwarded variant values are collapsed into one line with a placeholder (on a terminal
        # the repeated `gpu-dep` is elided, and --by-when puts the condition in a header)
        (
            ["many-conditional-deps"],
            [
                r"\+cuda cuda_arch=\*\s+build, link",
                r"when \+cuda cuda_arch=\*",
                r"\(\* = one of 30 cuda_arch values\)",
                r"\+rocm amdgpu_target=\*\s+build, link",
                r"when \+rocm amdgpu_target=\*",
                # version pins differ per value and are not collapsed
                r"@7\s+build, link",
                r"when @1\.0\+cuda cuda_arch=7",
            ],
            [r"cuda_arch=7\s+build"],
        ),
        # languages are not listed as dependencies but in the label block
        (["bowtie"], [r"\nLanguages:\s+c\n", r"Dependencies:\n    None"], [r"\n    c\s+build"]),
        (["gcc"], [r"\nLanguages:\s+c, cxx\n"], []),
        (["many-conditional-deps"], [r"\nLanguages:\s+c, fortran \(when \+fortran\)\n"], []),
        (["many-conditional-deps~fortran"], [r"\nLanguages:\s+c\n"], []),
        # virtual dependencies are marked
        (["optional-dep-test"], [r"\n    mpi \(virtual\)\s+build, link"], []),
        # Ensure spack info knows that build_system is a single value variant
        (
            ["dual-cmake-autotools"],
            [r"when\s*build_system=mock_cmake", r"when\s*build_system=mock_autotools"],
            [],
        ),
        (
            ["dual-cmake-autotools build_system=mock_cmake"],
            [r"when\s*build_system=mock_cmake"],
            [r"when\s*build_system=mock_autotools"],
        ),
        # Ensure that gemerator=make implies build_system=mock_cmake and therefore no autotools
        (
            ["dual-cmake-autotools generator=make"],
            [r"when\s*build_system=mock_cmake"],
            [r"when\s*build_system=mock_autotools"],
        ),
        (
            ["optional-dep-test"],
            [
                r"when \^pkg-g",
                r"when \%intel",
                r"when \%intel\@64\.1",
                r"when \%clang@34\:40",
                r"when \^pkg\-f",
            ],
            [],
        ),
    ],
)
@pytest.mark.parametrize("by_name", [True, False])
@pytest.mark.parametrize("layout", ["terminal", "pipe"])
def test_info_output(layout, by_name, args, in_output, not_in_output, request):
    request.getfixturevalue(layout)
    by_name_arg = ["--by-name"] if by_name else ["--by-when"]
    output = info(*(by_name_arg + args))

    for io in in_output:
        assert re.search(io, output), f"pattern {io!r} not found in output:\n{output}"
    for nio in not_in_output:
        assert not re.search(nio, output), f"pattern {nio!r} found in output:\n{output}"


def test_pipe_layout_is_one_line_per_entry(pipe):
    """In a pipe, a variant with a condition and allowed values is a single line."""
    output = info("dual-cmake-autotools")
    line = next(line for line in output.splitlines() if line.startswith("    generator"))
    assert re.fullmatch(
        r"    generator \[make\]\s+the build system generator to use\s+"
        r"when build_system=mock_cmake\s+one of: make, ninja",
        line,
    )
    assert "\n\n  when" not in output  # no grouping by condition in --by-name mode


def test_terminal_layout(terminal):
    """On a terminal, details are listed under the description in the same column."""
    output = info("dual-cmake-autotools")
    assert re.search(
        r"^    generator \[make\](\s+)the build system generator to use\n"
        r"(\s+)when build_system=mock_cmake\n"
        r"\2one of: make, ninja\n",
        output,
        re.M,
    )
    # a variant with allowed values but no condition lists the values right below
    assert re.search(
        r"^    build_system \[mock_autotools\]\s+Build systems supported by the package\n"
        r"\s+one of: mock_autotools, mock_cmake\n"
        r"    generator ",
        output,
        re.M,
    )
    assert "false, true" not in output


def test_terminal_layout_wraps_overlong_names(terminal):
    output = info("long-boost-dependency+longdep")
    # the dependency spec is too long for the name column: the rest moves to the next line
    assert re.search(
        r"^    boost\+atomic\+chrono\+date_time\+filesystem\+graph\+iostreams\+locale\n"
        r"\s+build, link\s+when \+longdep\n",
        output,
        re.M,
    )


def test_terminal_layout_repeats_dependency_name_once(terminal):
    output = info("many-conditional-deps")
    # `gpu-dep` is printed for the first entry only; later entries are aligned under it
    assert re.search(
        r"^    gpu-dep\+rocm amdgpu_target=\*\s+build, link.*\n"
        r"(.*\n)*"
        r"^           \+cuda cuda_arch=\*\s+build, link",
        output,
        re.M,
    )
    assert output.count("gpu-dep") == 1


def test_by_when_groups_entries(pipe):
    output = info("--by-when", "dual-cmake-autotools")
    dependencies = output[output.index("Dependencies:") :]
    # one header per condition, entries beneath it without their own `when`
    assert re.search(
        r"^  when build_system=mock_cmake\n    cmake@3\.5\.1:\s+build\n", dependencies, re.M
    )
    assert "cmake@3.5.1:    build  when" not in dependencies


def test_all_shows_urls(pipe):
    assert "tar.bz2" not in info("bowtie")
    output = info("--all", "bowtie")
    assert re.search(r"^    1\.4\.0\s+http://bowtie-1\.4\.0\.tar\.bz2$", output, re.M)


@pytest.mark.parametrize(
    "text,needle,expected",
    [
        ("kokkos+cuda cuda_arch=10", "cuda_arch=10", "kokkos+cuda cuda_arch=*"),
        # must not match a prefix of a longer value
        ("kokkos+cuda cuda_arch=100", "cuda_arch=10", "kokkos+cuda cuda_arch=100"),
        # value followed by a color code, or by more constraints
        (
            "\x1b[0;94m+cuda cuda_arch=10\x1b[0m",
            "cuda_arch=10",
            "\x1b[0;94m+cuda cuda_arch=*\x1b[0m",
        ),
        ("@1.0 cuda_arch=10 +mpi", "cuda_arch=10", "@1.0 cuda_arch=* +mpi"),
        # not inside a dependency name
        ("^py-cuda_arch=10 cuda_arch=10", "cuda_arch=10", "^py-cuda_arch=10 cuda_arch=*"),
    ],
)
def test_replace_component(text, needle, expected):
    assert spack.cmd.info._replace_component(text, needle, "cuda_arch=*") == expected


@pytest.mark.parametrize(
    "values,expected",
    [
        (["cuda_arch=80", "cuda_arch=90", "cuda_arch=90a"], "cuda_arch={80,90,90a}"),
        (["cuda_arch=100", "cuda_arch=101"], "cuda_arch={100,101}"),
        (["%c", "%cxx", "%fortran"], "%{c,cxx,fortran}"),
        (["%clang@11.0.1", "%clang@12.0.1"], "%clang@{11.0.1,12.0.1}"),
        (["@:16", "@18:"], "@{:16,18:}"),
        (["target=aarch64:", "target=ppc64le:"], "target={aarch64:,ppc64le:}"),
        (["^pkg-f", "^pkg-g"], "^{pkg-f,pkg-g}"),
    ],
)
def test_braces(values, expected):
    assert spack.cmd.info._braces(values) == expected


def test_merge_conditions(pipe):
    output = info("many-conditional-deps")
    # the same dependency under conditions differing in one value is listed once
    assert re.search(
        r"^    gpu-dep@:1\s+build, link\s+when @1\.0:\+cuda cuda_arch=\{0,1,2,3,4,5,6,7,8,9,"
        r"10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29\}$",
        output,
        re.M,
    )
    assert output.count("gpu-dep@:1") == 1
    # ... but not when the conditions differ in more than one component
    output = info("optional-dep-test")
    assert re.search(
        r"^    mpi \(virtual\)\s+build, link\s+when \^pkg-g\n"
        r"    mpi \(virtual\)\s+build, link\s+when \+mpi",
        output,
        re.M,
    )


def test_installed_row(pipe, database):
    output = info("mpich")
    assert re.search(
        r"^Installed:\s+mpich@3\.0\.4/\w{7}  \(see: spack find -lv mpich\)$", output, re.M
    )
    assert re.search(r"^Installed:\s+none$", info("mpich@1.0"), re.M)


def test_externals_and_preferences_rows(pipe, config):
    output = info("externaltool")
    assert re.search(
        r"^Externals:\s+externaltool@1\.0 at /path/to/external_tool\n\s+externaltool@0\.9 at /usr\n",
        output,
        re.M,
    )
    assert re.search(r"^Preferences:\s+buildable: false$", output, re.M)
    # only externals matching the spec
    assert "externaltool@1.0" not in info("externaltool@0.9")
    assert "Externals:" not in info("mpich")


def test_conflicts_requirements_patches(pipe):
    output = info("bowtie")
    assert re.search(r"^Constraints:\s+3 conflicts  \(list with --conflicts\)$", output, re.M)
    assert "Conflicts:" not in output

    output = info("--conflicts", "bowtie")
    assert re.search(
        r"^Conflicts:\n"
        r"    %gcc\s+when @1\.3  \(not for @1\.4\.0\)\n"
        r"    %gcc@:4\.5\.0\s+when @1\.2\.2  \(not for @1\.4\.0\)\n"
        r"    %gcc@:10\.2\.1\s+when @:1\.2\.9  \(not for @1\.4\.0\)\n",
        output,
        re.M,
    )
    # conflicts are filtered by the spec like everything else
    assert "%gcc@:4.5.0" not in info("--conflicts", "bowtie@1.4")

    output = info("--conflicts", "requires-clang")
    assert re.search(
        r"^Requirements:\n    %clang\s+can only be compiled with Clang$", output, re.M
    )

    output = info("patch")
    assert re.search(r"^Patches:\s+4  \(list with --patches\)$", output, re.M)
    output = info("--patches", "patch")
    assert re.search(
        r"^Patches:\n    foo\.patch\n    bar\.patch\s+when @2:\n    baz\.patch\n"
        r"    biz\.patch\s+when @1\.0\.1:1\.0\.2  \(not for @2\.0\)\n",
        output,
        re.M,
    )


def test_merge_conflicts_sharing_a_condition(pipe):
    output = info("--conflicts", "many-conditional-deps")
    assert re.search(r"^    \+\{cuda,rocm\}\s+when @:0\.9  \(not for @1\.0\)$", output, re.M)
    assert re.search(r"^    \+cuda\s+pick one GPU backend\s+when \+rocm$", output, re.M)


def test_terminal_layout_keeps_when_with_its_condition(terminal):
    """An unbreakable condition longer than the remaining width stays on the `when` line."""
    output = info("many-conditional-deps")
    # the brace group is too long for the line and wraps as a whole, `when` stays put
    assert re.search(r"when @1\.0:\+cuda\n\s+cuda_arch=\{0,1,2,", output)
    assert not re.search(r"when\n", output)


def test_entries_for_other_versions(pipe, terminal, monkeypatch):
    """Entries that cannot apply to the version the spec resolves to are tagged in a pipe and
    rendered faint on a terminal."""
    monkeypatch.setattr(spack.cmd.info, "_stdout_is_tty", lambda: False)
    output = info("dual-cmake-autotools")
    assert re.search(
        r"^    cmake@3\.14\.0:\s+build\s+when @2\.1\.0: build_system=mock_cmake  "
        r"\(not for @1\.0\)$",
        output,
        re.M,
    )
    assert re.search(r"^    cmake@3\.5\.1:\s+build\s+when build_system=mock_cmake$", output, re.M)

    monkeypatch.setattr(spack.cmd.info, "_stdout_is_tty", lambda: True)
    with spack.util.tty.color.color_when(True):
        output = info("dual-cmake-autotools")
    faint = [line for line in output.splitlines() if line.startswith("\x1b[2m")]
    assert len(faint) == 1 and "@3.14.0:" in faint[0] and "(not for" not in output
