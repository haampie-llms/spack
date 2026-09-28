# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Tests for Starlark package recipes (package.star)."""

import pathlib

import pytest

import spack.deptypes
import spack.directives_meta
import spack.repo
import spack.star_package
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
    "foo": f"""
load("//build_systems/lib.star", "triple")

package(description = "a foo", homepage = "https://foo.test", license = "MIT")
version("1.1", sha256 = "{SHA}", url = "https://foo.test/foo-1.1.tar.gz")
version("1.0-musl", sha256 = "{SHA}", url = "https://foo.test/foo-1.0.tar.gz")
build_system("generic")
depends_on("bar@2.0", "dash")
depends_on("baz", when = "@=1.1")
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
package(description = "a bar")
version("3.0")
version("2.0")
""",
    "baz": """
package(description = "a baz")
version("5")
version("4")
""",
    "dash": """
package(description = "a shell")
version("1")
""",
}

SELFISH = {
    "selfish": """
package(description = "depends on itself")
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
    monkeypatch.setattr(spack.star_package, "_records", {})
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
    assert cls.homepage == "https://foo.test"
    assert "build-tools" in cls.tags
    assert {str(v) for v in cls.versions} == {"1.1", "1.0-musl"}

    deps = cls.dependencies_by_name(when=True)
    # name@version pins exactly; a bare name is the first version its recipe declares
    assert {str(d.spec) for dl in deps["bar"].values() for d in dl} == {"bar@=2.0"}
    assert {str(d.spec) for dl in deps["baz"].values() for d in dl} == {"baz@=5"}
    assert all(
        d.depflag == spack.deptypes.BUILD
        for by_when in deps.values()
        for dl in by_when.values()
        for d in dl
    )
    assert cls._star_patches == [("x.patch", 1, "@=1.0-musl")]


def test_star_package_self_dependency_is_an_error(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setattr(spack.star_package, "_records", {})
    repo = _star_repo(tmp_path, SELFISH)
    with spack.repo.use_repositories(repo):
        with pytest.raises(spack.repo.RepoError, match="depends on itself"):
            repo.get_pkg_class("selfish")
        # nothing of the refused recipe leaks into the next package class
        assert not spack.directives_meta.DirectiveMeta._directives_to_be_executed


def test_star_source_hash_covers_loaded_modules(star_repo):
    text = spack.star_package.source_hash(star_repo.filename_for_package_name("foo"))
    assert "def install" in text and "def triple" in text


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


def test_star_directives_only_while_loading(star_repo):
    recipe = pathlib.Path(star_repo.root) / "packages" / "bar" / "package.star"
    recipe.write_text(
        recipe.read_text() + "\ndef install(ctx):\n    version('9')\n    return []\n"
    )
    packages = str(pathlib.Path(star_repo.root) / "packages")
    ctx = {"version": "3.0", "arch": "amd64", "id": "bar-3.0", "deps": {}}
    with pytest.raises(spack.starlark_eval.StarlarkError, match="only be called while"):
        spack.starlark_eval.plan(packages, star_repo.root, "bar", ctx)
