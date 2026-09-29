# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Tests for Starlark package recipes (package.star)."""

import pathlib

import pytest

import spack.deptypes
import spack.directives_meta
import spack.repo
import spack.spec
import spack.star_recipe
import spack.starlark_eval
import spack.util.file_cache

SHA = "0" * 64

LIB = """
def triple(ctx, libc = "gnu"):
    return {"amd64": "x86_64", "aarch64": "aarch64"}[ctx.arch] + "-linux-" + libc
"""

GENERIC = """
phases = ["edit", "install"]

def edit(ctx):
    return []

def install(ctx):
    fail("generic requires install()")
"""

RECIPES = {
    "foo": f"""\"\"\"a
    foo\"\"\"

load("//build_systems/lib.star", "triple")

homepage = "https://foo.test"
parallel = False
license("MIT")
version("1.1", sha256 = "{SHA}", url = "https://foo.test/foo-1.1.tar.gz")
version("1.0-musl", sha256 = "{SHA}", url = "https://foo.test/foo-1.0.tar.gz")
build_system(conditional("generic", when = "@=1.1,=1.0-musl"))
depends_on("bar@2.0")
depends_on("dash", type = "build")
depends_on("baz", when = "@=1.1", type = ("build", "run"))
patch("x.patch", when = "@=1.0-musl")

def install(ctx):
    return [
        mkdir(ctx.prefix + "/bin"),
        write_file(ctx.prefix + "/bin/foo", "#!" + ctx.sh + "\\necho " + triple(ctx) + "\\n",
                   mode = "755"),
        run("make", ctx.makejobs, "install", env = {{"V": "1"}}),
    ] + ([sh("true")] if ctx.satisfies("@=1.1") else [])
""",
    "bar": """
"a bar"
version("3.0")
version("2.0")
""",
    "baz": """
"a baz"
version("5")
version("4")
""",
    "dash": """
"a shell"
version("1")
""",
}

SELFISH = {
    "selfish": """
"depends on itself"
version("2")
depends_on("selfish@1")
"""
}


def _star_repo(tmp_path: pathlib.Path, recipes) -> spack.repo.Repo:
    """A Package API v1 repo of package.star recipes, with shpack's layout (the
    build systems in build_systems/ beside packages/)."""
    root, _ = spack.repo.create_repo(
        str(tmp_path / "repo"), namespace="starry", package_api=(1, 0)
    )
    for name, text in recipes.items():
        d = pathlib.Path(root) / "packages" / name
        d.mkdir(parents=True)
        (d / "package.star").write_text(text)
    bs = pathlib.Path(root) / "build_systems"
    bs.mkdir()
    (bs / "lib.star").write_text(LIB)
    (bs / "generic.star").write_text(GENERIC)
    return spack.repo.Repo(root, cache=spack.util.file_cache.FileCache(str(tmp_path / "cache")))


@pytest.fixture()
def star_repo(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setattr(spack.star_recipe, "records_cache", {})
    repo = _star_repo(tmp_path, RECIPES)
    (pathlib.Path(repo.root) / "packages" / "foo" / "patches").mkdir()
    (pathlib.Path(repo.root) / "packages" / "foo" / "patches" / "x.patch").write_text("")
    with spack.repo.use_repositories(repo):
        yield repo


def test_star_packages_are_discovered(star_repo):
    assert set(star_repo.all_package_names()) == set(RECIPES)
    assert star_repo.filename_for_package_name("foo").endswith("package.star")


def test_star_package_class(star_repo):
    cls = star_repo.get_pkg_class("foo")
    assert cls.name == "foo"
    assert cls.__doc__ == "a foo"
    assert cls.homepage == "https://foo.test"
    assert cls.parallel is False
    assert list(cls.licenses.values()) == ["MIT"]
    assert "build-tools" in cls.tags
    assert {str(v) for v in cls.versions} == {"1.1", "1.0-musl"}

    deps = cls.dependencies_by_name(when=True)
    # name@version pins exactly; a bare name is the first version its recipe declares
    assert {str(d.spec) for dl in deps["bar"].values() for d in dl} == {"bar@=2.0"}
    assert {str(d.spec) for dl in deps["baz"].values() for d in dl} == {"baz@=5"}
    # to the concretizer every edge is a build edge (it cannot duplicate the
    # bootstrap's link dependencies); the recipe's types are kept for the build
    assert all(
        d.depflag == spack.deptypes.BUILD
        for by_when in deps.values()
        for dl in by_when.values()
        for d in dl
    )
    assert [(spec, types) for _, spec, types in cls._star_deps] == [
        ("bar@2.0", ("build", "link")),
        ("dash", ("build",)),
        ("baz", ("build", "run")),
    ]
    assert cls._star_patches == [("x.patch", 1, "@=1.0-musl")]


def test_star_package_self_dependency_is_an_error(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setattr(spack.star_recipe, "records_cache", {})
    repo = _star_repo(tmp_path, SELFISH)
    with spack.repo.use_repositories(repo):
        with pytest.raises(spack.repo.RepoError, match="depends on itself"):
            repo.get_pkg_class("selfish")
        # nothing of the refused recipe leaks into the next package class
        assert not spack.directives_meta.DirectiveMeta._directives_to_be_executed


def test_star_source_hash_is_shpacks_package_text(star_repo):
    """What the package hash sees of a recipe is shpack's package text: the version and
    arch, every file of the package directory and every loaded module, by content."""
    spec = spack.spec.Spec("foo@=1.1 target=aarch64")
    text = spack.star_recipe.source_hash(spec, star_repo.filename_for_package_name("foo"))
    lines = text.splitlines()
    assert lines[:3] == ["package foo", "version 1.1", "arch aarch64"]
    files = [line.split()[2] for line in lines if line.startswith("file ")]
    assert files == ["package.star", "patches/x.patch"]
    assert "evaluator star 1.0" in lines
    # the modules it loads, its build system's included
    loads = {line.split()[2] for line in lines if line.startswith("load ")}
    assert loads == {"build_systems/lib.star", "build_systems/generic.star"}


def test_star_plan(star_repo):
    ctx = {
        "name": "foo",
        "version": "1.1",
        "id": "foo-1.1",
        "arch": "aarch64",
        "prefix": "/p",
        "sh": "/sh",
        "stage_dir": "/s",
        "source_dir": "/s/foo-1.1",
        "package_dir": "/r/foo",
        "jobs": 2,
        "makejobs": [],
        "file_prefix_map": "",
        "debug_prefix_map": "",
        "package_files": ["package.star"],
        "deps": {"dash": "/d"},
    }
    packages = str(pathlib.Path(star_repo.root) / "packages")
    plan = spack.starlark_eval.plan(packages, star_repo.root, "foo", ctx)
    assert [p["phase"] for p in plan["phases"]] == ["edit", "install"]
    assert plan["phases"][1]["actions"] == [
        {"op": "mkdir", "paths": ["/p/bin"]},
        {
            "content": "#!/sh\necho aarch64-linux-gnu\n",
            "mode": "755",
            "op": "write_file",
            "path": "/p/bin/foo",
        },
        {"argv": ["make", "install"], "env": {"V": "1"}, "op": "run"},
        {"op": "sh", "script": "true"},
    ]


def test_star_record(star_repo):
    """The record matches star's: attributes from the docstring and globals, one
    depends_on per spec with canonical types, Spack's build_system values."""
    packages = str(pathlib.Path(star_repo.root) / "packages")
    rec = spack.starlark_eval.recipe(packages, star_repo.root, "foo")
    assert list(rec)[:5] == ["name", "description", "homepage", "parallel", "build_directory"]
    assert rec["description"] == "a foo" and rec["parallel"] is False
    kinds = [d["directive"] for d in rec["directives"]]
    assert kinds == ["license", "version", "version", "build_system"] + ["depends_on"] * 3 + [
        "patch"
    ]
    assert rec["directives"][3] == {
        "directive": "build_system",
        "values": [{"name": "generic", "when": "@=1.1,=1.0-musl"}],
        "default": "generic",
    }
    assert rec["directives"][6] == {
        "directive": "depends_on",
        "spec": "baz",
        "type": ["build", "run"],
        "when": "@=1.1",
    }


@pytest.mark.parametrize(
    "text,error",
    [
        ('depends_on("a", "b")', "only @=VERSION"),
        ('depends_on("a", type = "runtime")', "want build, link, run or test"),
        ('depends_on("a", when = "@1:")', "version ranges are not supported"),
        ('version("1")\nbuild_system("generic")\nbuild_system("generic")', "more than once"),
        ("parallel = 0", "parallel must be a bool"),
    ],
)
def test_star_record_errors(star_repo, text, error):
    recipe = pathlib.Path(star_repo.root) / "packages" / "bar" / "package.star"
    recipe.write_text(text + "\n")
    packages = str(pathlib.Path(star_repo.root) / "packages")
    with pytest.raises((spack.starlark_eval.StarlarkError, TypeError), match=error):
        spack.starlark_eval.recipe(packages, star_repo.root, "bar")


def test_star_when(star_repo):
    """when(cond, [...]) ANDs cond into the directives it is given, nests, and
    canonicalizes the result."""
    recipe = pathlib.Path(star_repo.root) / "packages" / "bar" / "package.star"
    recipe.write_text(
        """
version("1")
version("2")
version("3")
when("@=1,=2", [
    depends_on("baz", type = "build"),
    depends_on("dash", when = "@=2,=3"),
    when("target=aarch64:", [patch("x.patch")]),
])
x = when("@=3", [license(n) for n in ["MIT", "BSD-3-Clause"]])
"""
    )
    packages = str(pathlib.Path(star_repo.root) / "packages")
    rec = spack.starlark_eval.recipe(packages, star_repo.root, "bar")
    assert [d.get("when") for d in rec["directives"]] == [
        None,
        None,
        None,
        "@=1,=2",
        "@=2",
        "@=1,=2 target=aarch64:",
        "@=3",
        "@=3",
    ]


@pytest.mark.parametrize(
    "text,error",
    [
        ('when("@=1", [version("2")])', "want the value of depends_on"),
        ('when("@=1", [depends_on("a", when = "@=2")])', "no version satisfies both"),
        ('when("target=aarch64:", [depends_on("a")])', "only supported on patch"),
        ('when("@1:", [depends_on("a")])', "version ranges are not supported"),
    ],
)
def test_star_when_errors(star_repo, text, error):
    recipe = pathlib.Path(star_repo.root) / "packages" / "bar" / "package.star"
    recipe.write_text('version("1")\n' + text + "\n")
    packages = str(pathlib.Path(star_repo.root) / "packages")
    with pytest.raises(spack.starlark_eval.StarlarkError, match=error):
        spack.starlark_eval.recipe(packages, star_repo.root, "bar")


def test_star_directives_only_while_loading(star_repo):
    recipe = pathlib.Path(star_repo.root) / "packages" / "bar" / "package.star"
    recipe.write_text(
        recipe.read_text() + "\ndef install(ctx):\n    version('9')\n    return []\n"
    )
    packages = str(pathlib.Path(star_repo.root) / "packages")
    ctx = {"version": "3.0", "arch": "amd64", "id": "bar-3.0", "deps": {}}
    with pytest.raises(spack.starlark_eval.StarlarkError, match="only be called while"):
        spack.starlark_eval.plan(packages, star_repo.root, "bar", ctx)
