# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Package recipes written in Starlark (``package.star``).

A ``package.star`` is a recipe in the format of shpack's recipes (see
``star/PROTOCOL.md`` in shpack): directives declare versions, dependencies and
patches, and phase functions return *actions* instead of performing them.
Evaluation is pure and happens in an external backend -- any program that
implements the protocol; by default the ``star`` binary, found through the
``SPACK_STAR`` environment variable or on ``PATH``:

* ``star recipe --format json`` gives the directive record, from which this
  module builds an ordinary :class:`spack.package_base.PackageBase` subclass
  (so concretization sees versions, dependencies and patches as usual);
* at install time, ``star plan --format json`` evaluates the phases against a
  build context derived from the concrete spec, and the resulting actions are
  executed here with Spack's own file utilities.

``resource()`` keeps shpack's semantics (unpacked flat into the stage, beside the
source) by fetching in the install step rather than through Spack's resources,
so ``spack mirror`` does not see them yet.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tarfile
import types
from typing import Any, Dict, List

import spack.builder
import spack.config
import spack.directives
import spack.fetch_strategy
import spack.stage
import spack.util.naming as nm
from spack.error import SpackError
from spack.util import tty
from spack.util.filesystem import copy_tree, filter_file, mkdirp

STAR_FILE_NAME = "package.star"

#: shpack's arch names, by target family
_ARCH = {"x86_64": "amd64", "aarch64": "aarch64"}


class StarError(SpackError):
    """Error evaluating or executing a Starlark package recipe."""


def star_executable() -> str:
    exe = os.environ.get("SPACK_STAR") or shutil.which("star")
    if not exe:
        raise StarError(
            "cannot evaluate package.star recipes: set SPACK_STAR or put `star` on PATH"
        )
    return exe


def _star(*args: str) -> Any:
    proc = subprocess.run(
        [star_executable(), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
        raise StarError(f"star {args[0]} failed", proc.stderr.decode("utf-8", "replace"))
    return json.loads(proc.stdout.decode("utf-8"))


def _exact(spec: str) -> str:
    """shpack's ``name@version`` pins one exact version; Spack writes that ``@=``."""
    name, at, version = spec.partition("@")
    return f"{name}@={version}" if at and not version.startswith("=") else spec


def make_package_class(repo, pkg_name: str, filename: str) -> type:
    """Build the package class for ``pkg_name`` from its ``package.star``."""
    pkg_dir = os.path.dirname(filename)
    packages_path = os.path.dirname(pkg_dir)
    root = os.path.dirname(packages_path)  # load("//...") resolves here
    record = _star(
        "recipe",
        "--repo",
        packages_path,
        "--root",
        root,
        "--format",
        "json",
        os.path.basename(pkg_dir),
    )

    module_name = f"{repo.full_namespace}.{repo.naming_scheme.pkg_name_to_pkg_dir(pkg_name)}"
    module = types.ModuleType(module_name)
    module.__file__ = filename
    sys.modules[module_name] = module

    attrs: Dict[str, Any] = {
        "__module__": module_name,
        "__doc__": record["package"]["description"],
        "_star_file": filename,
        "_star_root": root,
        "_star_packages": packages_path,
        "install": _install,
    }
    if record["package"]["homepage"]:
        attrs["homepage"] = record["package"]["homepage"]

    # Directives queue up and are consumed by the next package class created,
    # so every one of them is called right before the class below.
    has_code = False
    first_url = None
    resources = []
    for d in record["directives"]:
        kind = d["directive"]
        when = d.get("when")
        if kind == "version":
            kwargs = {}
            if d["sha256"]:
                kwargs["sha256"] = d["sha256"]
                has_code = True
            if d["url"]:
                kwargs["url"] = d["url"]
                first_url = first_url or d["url"]
            spack.directives.version(d["version"], **kwargs)
        elif kind == "depends_on":
            if d["spec"].partition("@")[0] == pkg_name:
                # shpack builds a package with an older version of itself
                # (gcc-boot@9.5.0 with gcc-boot@4.7); Spack has no
                # self-dependencies, so such an edge cannot be expressed.
                tty.debug(f"{pkg_name}: dropping self-dependency {d['spec']}")
                continue
            spack.directives.depends_on(_exact(d["spec"]), when=when)
        elif kind == "patch":
            spack.directives.patch(os.path.join("patches", d["file"]), level=d["level"], when=when)
        elif kind == "parallel":
            attrs["parallel"] = d["value"]
        elif kind in ("build_system", "build_directory"):
            pass  # the plan carries these
        elif kind == "resource":
            resources.append((when, d["sha256"], d["url"], d["fname"]))
    if record["package"]["license"]:
        spack.directives.license(record["package"]["license"])
    if first_url:
        attrs["url"] = first_url
    if not has_code:
        attrs["has_code"] = False
    attrs["_star_resources"] = resources

    base = spack.builder.Package
    cls = type(base)(nm.pkg_name_to_class_name(pkg_name), (base,), attrs)  # type: ignore[misc]
    setattr(module, cls.__name__, cls)
    return cls


# --------------------------------------------------------------------- install


def _ctx(pkg, spec, prefix) -> Dict[str, Any]:
    stage_dir = pkg.stage.path
    package_dir = os.path.dirname(pkg._star_file)
    deps: Dict[str, str] = {}
    for dep in spec.traverse(root=False, order="breadth"):
        deps.setdefault(dep.name, dep.prefix)
    sh = os.path.join(deps["dash"], "bin", "sh") if "dash" in deps else "/bin/sh"
    files = []
    for root, _, names in os.walk(package_dir):
        for n in names:
            if not n.startswith("."):
                files.append(os.path.relpath(os.path.join(root, n), package_dir))
    jobs = spack.config.determine_number_of_jobs(parallel=pkg.parallel)
    return {
        "name": spec.name,
        "version": str(spec.version),
        "id": f"{spec.name}-{spec.version}",
        "arch": _ARCH.get(str(spec.target.family), str(spec.target.family)),
        "prefix": str(prefix),
        "sh": sh,
        "stage_dir": stage_dir,
        "source_dir": pkg.stage.source_path,
        "package_dir": package_dir,
        "jobs": jobs,
        # shpack leaves -j to an inherited jobserver; here make is whatever is on PATH (its
        # recipes do not depend on it), which may not speak Spack's fifo jobserver, so the
        # plan gets an explicit -jN and the inherited MAKEFLAGS are dropped (_install).
        "makejobs": [f"-j{jobs}" if pkg.parallel else "-j1"],
        "file_prefix_map": f"-ffile-prefix-map={stage_dir}=.",
        "debug_prefix_map": f"-fdebug-prefix-map={stage_dir}=.",
        "package_files": sorted(files),
        "deps": deps,
    }


def _starlark_literal(value) -> str:
    """ctx as Starlark source: JSON is valid Starlark for these types."""
    return json.dumps(value, indent=4, sort_keys=False)


def _fetch_resources(pkg, spec) -> None:
    """Unpack the recipe's resources into the stage, beside the source (as shpack does)."""
    for when, sha256, url, fname in pkg._star_resources:
        if when and not spec.satisfies(when):
            continue
        fetcher = spack.fetch_strategy.URLFetchStrategy(url=url, checksum=sha256)
        with spack.stage.stage_from_config(
            fetcher, config=spack.config.CONFIG, name=f"{spec.name}-resource-{sha256[:7]}"
        ) as stage:
            stage.fetch()
            stage.check()
            with tarfile.open(stage.archive_file) as tar:
                tar.extractall(pkg.stage.path)


def _install(self, spec, prefix):
    os.environ.pop("MAKEFLAGS", None)
    _fetch_resources(self, spec)
    ctx = _ctx(self, spec, prefix)
    ctx_file = os.path.join(self.stage.path, "ctx.star")
    with open(ctx_file, "w", encoding="utf-8") as f:
        f.write("ctx = " + _starlark_literal(ctx) + "\n")
    plan = _star(
        "plan",
        "--repo",
        self._star_packages,
        "--root",
        self._star_root,
        "--ctx",
        ctx_file,
        "--format",
        "json",
        os.path.basename(os.path.dirname(self._star_file)),
    )
    executor = _Executor(ctx, os.getcwd())
    for phase in plan["phases"]:
        for action in phase["actions"]:
            executor.do(action)


class _Executor:
    """Performs plan actions with the same semantics as shpack's sh renderer."""

    def __init__(self, ctx: Dict[str, Any], cwd: str):
        self.ctx = ctx
        self.cwd = cwd

    def path(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(self.cwd, p)

    def expand(self, p: str, must_match: bool = True) -> List[str]:
        if not any(c in p for c in "*?["):
            return [self.path(p)]
        matches = sorted(glob.glob(self.path(p), recursive="**" in p))
        if not matches and must_match:
            raise StarError(f"no file matches {p}")
        return matches

    def one(self, p: str) -> str:
        m = self.expand(p)
        if len(m) != 1:
            raise StarError(f"{p} matches {len(m)} paths, want exactly one")
        return m[0]

    def do(self, a: Dict[str, Any]) -> None:
        getattr(self, "_" + a["op"])(a)

    def _run(self, a):
        env = dict(os.environ)
        env.update(a.get("env") or {})
        cwd = self.path(a["cwd"]) if a.get("cwd") else self.cwd
        out = open(self.path(a["stdout"]), "wb") if a.get("stdout") else None
        try:
            subprocess.run(a["argv"], cwd=cwd, env=env, stdout=out, check=True)
        finally:
            if out:
                out.close()

    def _sh(self, a):
        cwd = self.path(a["cwd"]) if a.get("cwd") else self.cwd
        subprocess.run([self.ctx["sh"], "-ec", a["script"]], cwd=cwd, check=True)

    def _setenv(self, a):
        os.environ[a["name"]] = a["value"]

    def _prepend_path(self, a):
        old = os.environ.get(a["name"])
        os.environ[a["name"]] = a["value"] + (":" + old if old else "")

    def _unsetenv(self, a):
        os.environ.pop(a["name"], None)

    def _chdir(self, a):
        self.cwd = self.one(a["path"])
        os.chdir(self.cwd)

    def _mkdir(self, a):
        for p in a["paths"]:
            mkdirp(self.path(p))

    def _copy(self, a):
        dst = self.one(a["dst"]) if any(c in a["dst"] for c in "*?[") else self.path(a["dst"])
        for pattern in a["src"]:
            for src in self.expand(pattern):
                self._copy_one(src, dst, a)

    def _copy_one(self, src, dst, a):
        recursive = a.get("recursive") or a.get("preserve")
        contents = src.endswith("/.")
        if os.path.isdir(dst) and not contents:
            dst = os.path.join(dst, os.path.basename(src.rstrip("/")))
        if os.path.isdir(src):
            if not recursive:
                raise StarError(f"copy: {src} is a directory (recursive = True?)")
            copy_tree(src, dst, symlinks=bool(a.get("preserve")))
            return
        if a.get("force") and os.path.lexists(dst) and not os.access(dst, os.W_OK):
            os.remove(dst)
        (shutil.copy2 if a.get("preserve") else shutil.copy)(src, dst)

    def _move(self, a):
        dst = self.one(a["dst"]) if any(c in a["dst"] for c in "*?[") else self.path(a["dst"])
        for pattern in a["src"]:
            for src in self.expand(pattern):
                shutil.move(src.rstrip("/"), dst)

    def _remove(self, a):
        for pattern in a["paths"]:
            for p in self.expand(pattern, must_match=False):
                if os.path.isdir(p) and not os.path.islink(p):
                    if a.get("recursive"):
                        shutil.rmtree(p)
                    else:
                        raise StarError(f"remove: {p} is a directory (recursive = True?)")
                elif os.path.lexists(p):
                    os.remove(p)

    def _link(self, a, link_fn):
        link = self.path(a["link"])
        if os.path.lexists(link):
            if a.get("if_missing"):
                return
            if a.get("force"):
                os.remove(link)
        link_fn(a["target"], link)

    def _symlink(self, a):
        self._link(a, os.symlink)

    def _hardlink(self, a):
        self._link(a, lambda target, link: os.link(self.path(target), link))

    def _symlink_each(self, a):
        import fnmatch

        targets = []
        for pattern in a["targets"]:
            targets.extend(self.expand(pattern, must_match=False))
        for t in targets:
            if not os.path.lexists(t):
                continue
            base = os.path.basename(t)
            if a.get("exclude") and fnmatch.fnmatchcase(base, a["exclude"]):
                continue
            link = os.path.join(self.path(a["dir"]), (a.get("prefix") or "") + base)
            if os.path.lexists(link) and a.get("if_missing"):
                continue
            os.symlink(base if a.get("relative") else t, link)

    def _chmod(self, a):
        for pattern in a["paths"]:
            for p in self.expand(pattern):
                os.chmod(p, int(a["mode"], 8))

    def _write_file(self, a, mode="wb"):
        p = self.path(a["path"])
        with open(p, mode) as f:
            f.write(a["content"].encode("utf-8", "surrogateescape"))
        if a.get("mode"):
            os.chmod(p, int(a["mode"], 8))

    def _append_file(self, a):
        self._write_file(a, mode="ab")

    def _filter(self, files, regex, repl, string):
        paths: List[str] = []
        for pattern in files:
            paths.extend(self.expand(pattern))
        # repl is literal in the protocol; filter_file would read \1 and \\
        filter_file(regex, repl.replace("\\", "\\\\"), *paths, string=string, backup=False)

    def _substitute(self, a):
        self._filter(a["files"], a["old"], a["new"], string=True)

    def _filter_file(self, a):
        # the protocol's portable regex subset means the same in Python's re
        self._filter(a["files"], a["regex"], a["repl"], string=False)


def source_hash(filename: str) -> str:
    """What Spack's package hash sees of a Starlark recipe: its text and the text of every
    module it loads (build systems, helpers), as shpack hashes it."""
    pkg_dir = os.path.dirname(filename)
    packages_path = os.path.dirname(pkg_dir)
    root = os.path.dirname(packages_path)
    record = _star(
        "recipe",
        "--repo",
        packages_path,
        "--root",
        root,
        "--format",
        "json",
        os.path.basename(pkg_dir),
    )
    parts = []
    for path in [filename] + [os.path.join(root, m) for m in record["loads"]]:
        with open(path, "r", encoding="utf-8") as f:
            parts.append(f.read())
    return "\0".join(parts)
