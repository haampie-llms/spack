# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Tests for Starlark package recipes (package.star), with a fake evaluator."""

import json
import pathlib
import stat
import sys

import pytest

import spack.deptypes
import spack.directives_meta
import spack.repo
import spack.star_package
import spack.util.file_cache

SHA = "0" * 64

SELFISH = {
    "selfish": {
        "name": "selfish",
        "package": {"description": "depends on itself", "homepage": None, "license": None},
        "directives": [
            {"directive": "version", "version": "2", "sha256": None, "url": None, "fname": None},
            {"directive": "depends_on", "spec": "selfish@1", "when": None},
        ],
        "loads": [],
    }
}

RECORDS = {
    "foo": {
        "name": "foo",
        "package": {"description": "a foo", "homepage": "https://foo.test", "license": "MIT"},
        "directives": [
            {
                "directive": "version",
                "version": "1.1",
                "sha256": SHA,
                "url": "https://foo.test/foo-1.1.tar.gz",
                "fname": "foo-1.1.tar.gz",
            },
            {
                "directive": "version",
                "version": "1.0-musl",
                "sha256": SHA,
                "url": "https://foo.test/foo-1.0.tar.gz",
                "fname": "foo-1.0.tar.gz",
            },
            {"directive": "build_system", "name": "autotools", "when": None},
            {"directive": "depends_on", "spec": "bar@2.0", "when": None},
            {"directive": "depends_on", "spec": "baz", "when": "@=1.1"},
            {"directive": "patch", "file": "x.patch", "level": 1, "when": "@=1.0-musl"},
        ],
        "loads": ["build_systems/lib.star"],
    },
    "bar": {
        "name": "bar",
        "package": {"description": "a bar", "homepage": None, "license": None},
        "directives": [
            {"directive": "version", "version": "3.0", "sha256": None, "url": None, "fname": None},
            {"directive": "version", "version": "2.0", "sha256": None, "url": None, "fname": None},
        ],
        "loads": [],
    },
    "baz": {
        "name": "baz",
        "package": {"description": "a baz", "homepage": None, "license": None},
        "directives": [
            {"directive": "version", "version": "5", "sha256": None, "url": None, "fname": None},
            {"directive": "version", "version": "4", "sha256": None, "url": None, "fname": None},
        ],
        "loads": [],
    },
}


def _star_repo(tmp_path: pathlib.Path, monkeypatch, records):
    """A Package API v1 repo of package.star recipes, and a fake `star` that
    answers `star recipe` from ``records``."""
    root, _ = spack.repo.create_repo(
        str(tmp_path / "repo"), namespace="starry", package_api=(1, 0)
    )
    for name in records:
        d = pathlib.Path(root) / "packages" / name
        d.mkdir(parents=True)
        (d / "package.star").write_text(f"# {name}\n")
    (pathlib.Path(root) / "build_systems").mkdir()
    (pathlib.Path(root) / "build_systems" / "lib.star").write_text("# lib\n")

    star = tmp_path / "star"
    star.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"RECORDS = json.loads({json.dumps(json.dumps(records))})\n"
        "assert sys.argv[1] == 'recipe'\n"
        "print(json.dumps(RECORDS[sys.argv[-1]]))\n"
    )
    star.chmod(star.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("SPACK_STAR", str(star))
    monkeypatch.setattr(spack.star_package, "_records", {})
    return spack.repo.Repo(root, cache=spack.util.file_cache.FileCache(str(tmp_path / "cache")))


@pytest.fixture()
def star_repo(tmp_path: pathlib.Path, monkeypatch):
    repo = _star_repo(tmp_path, monkeypatch, RECORDS)
    (pathlib.Path(repo.root) / "packages" / "foo" / "patches").mkdir()
    (pathlib.Path(repo.root) / "packages" / "foo" / "patches" / "x.patch").write_text("")
    with spack.repo.use_repositories(repo):
        yield repo


def test_star_packages_are_discovered(star_repo):
    assert set(star_repo.all_package_names()) == set(RECORDS)
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
    repo = _star_repo(tmp_path, monkeypatch, SELFISH)
    with spack.repo.use_repositories(repo):
        with pytest.raises(spack.repo.RepoError, match="depends on itself"):
            repo.get_pkg_class("selfish")
        # nothing of the refused recipe leaks into the next package class
        assert not spack.directives_meta.DirectiveMeta._directives_to_be_executed


def test_star_source_hash_covers_loaded_modules(star_repo):
    text = spack.star_package.source_hash(star_repo.filename_for_package_name("foo"))
    assert "# foo" in text and "# lib" in text
