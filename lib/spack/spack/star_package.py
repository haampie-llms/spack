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
import shlex
import shutil
import subprocess
import sys
import types
from typing import Any, Dict, List

import spack.builder
import spack.config
import spack.directives
import spack.directives_meta
import spack.fetch_strategy
import spack.repo
import spack.stage
import spack.util.naming as nm
from spack.error import SpackError
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


_records: Dict[str, Any] = {}


def _record(packages_path: str, root: str, name: str) -> Any:
    key = os.path.join(packages_path, name)
    if key not in _records:
        _records[key] = _star(
            "recipe", "--repo", packages_path, "--root", root, "--format", "json", name
        )
    return _records[key]


def _resolve(packages_path: str, root: str, spec: str) -> str:
    """shpack's resolution, spelled for Spack: ``name@version`` pins that exact version,
    and a bare name means the first version its recipe declares (a recipe always beats
    an external); names without a recipe stay bare and resolve to an external."""
    name, at, version = spec.partition("@")
    if at:
        return f"{name}@={version}"
    if not os.path.exists(os.path.join(packages_path, name, STAR_FILE_NAME)):
        return spec
    for d in _record(packages_path, root, name)["directives"]:
        if d["directive"] == "version":
            return f"{name}@={d['version']}"
    return spec


def make_package_class(repo, pkg_name: str, filename: str) -> type:
    """Build the package class for ``pkg_name`` from its ``package.star``."""
    pkg_dir = os.path.dirname(filename)
    packages_path = os.path.dirname(pkg_dir)
    root = os.path.dirname(packages_path)  # load("//...") resolves here
    record = _record(packages_path, root, os.path.basename(pkg_dir))

    module_name = f"{repo.full_namespace}.{repo.naming_scheme.pkg_name_to_pkg_dir(pkg_name)}"
    module = types.ModuleType(module_name)
    module.__file__ = filename
    sys.modules[module_name] = module

    attrs: Dict[str, Any] = {
        "__module__": module_name,
        # Everything a recipe depends on is a build dependency (shpack builds several
        # versions of musl, grep, gawk, ... into one DAG); tagged build-tools, Spack lets
        # a package appear once per version in a DAG.
        "tags": ["build-tools"],
        "__doc__": record["package"]["description"],
        "_star_file": filename,
        "_star_root": root,
        "_star_packages": packages_path,
        "install": _install,
    }
    if record["package"]["homepage"]:
        attrs["homepage"] = record["package"]["homepage"]

    # Refuse what Spack cannot express before a single directive is queued: queued
    # directives go to the next package class created, whichever that is.
    for d in record["directives"]:
        if d["directive"] == "depends_on" and d["spec"].partition("@")[0] == pkg_name:
            raise StarError(f"{pkg_name} depends on itself ({d['spec']})")

    # Directives queue up and are consumed by the next package class created,
    # so every one of them is called right before the class below.
    try:
        return _make_class(repo, pkg_name, record, module, attrs, packages_path, root)
    except BaseException:
        spack.directives_meta.DirectiveMeta._directives_to_be_executed.clear()
        raise


def _make_class(repo, pkg_name, record, module, attrs, packages_path, root) -> type:
    has_code = False
    first_url = None
    resources = []
    star_deps: List = []
    star_patches: List = []
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
            # A recipe's dependencies are what its build sees (PATH, ctx.dep): build
            # dependencies to Spack, which lets two versions of a package in one DAG.
            spack.directives.depends_on(
                _resolve(packages_path, root, d["spec"]), when=when, type="build"
            )
            star_deps.append((when, d["spec"]))
        elif kind == "patch":
            spack.directives.patch(os.path.join("patches", d["file"]), level=d["level"], when=when)
            star_patches.append((d["file"], d["level"], when))
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
    attrs["_star_deps"] = star_deps
    attrs["_star_patches"] = star_patches

    base = spack.builder.Package
    cls = type(base)(nm.pkg_name_to_class_name(pkg_name), (base,), attrs)  # type: ignore[misc]
    setattr(module, cls.__name__, cls)
    return cls


# --------------------------------------------------------------------- install


def _declared_deps(node) -> List:
    """``node``'s dependencies in the order its recipe declares them (shpack's order),
    restricted to its version; externals and non-Starlark packages have none."""
    cls = spack.repo.PATH.get_pkg_class(node.fullname)
    if node.external or not hasattr(cls, "_star_deps"):
        return []
    out = []
    for when, dep_spec in cls._star_deps:
        if when and not node.satisfies(when):
            continue
        name = dep_spec.partition("@")[0]
        out.extend(node.dependencies(name=name))
    return out


def _closure_order(node) -> List:
    """shpack's PATH order for ``node``: a DFS post-order over declared dependencies
    (dependencies before dependents), which compose_path reverses."""
    order: List = []
    for dep in _declared_deps(node):
        for n in _closure_order(dep) + [dep]:
            if all(n.dag_hash() != o.dag_hash() for o in order):
                order.append(n)
    return order


def _node_id(node) -> str:
    return f"{node.name}-{node.version}"


def _ctx(pkg, spec, prefix, stage_dir: str, source_dir: str) -> Dict[str, Any]:
    package_dir = os.path.dirname(pkg._star_file)
    direct = _declared_deps(spec)
    closure = sorted(_closure_order(spec), key=_node_id)
    # name -> prefix over the direct deps, then the closure sorted by id: prefix_of
    deps: Dict[str, str] = {}
    for dep in direct + closure:
        deps.setdefault(dep.name, str(dep.prefix))
    names = [d.name for d in direct]
    shell_dep = "dash" if "dash" in names else "dash-boot" if "dash-boot" in names else None
    if shell_dep is None:
        raise StarError(f"{spec.name} declares no shell dependency (dash)")
    files = []
    for root, _, fnames in os.walk(package_dir):
        for n in fnames:
            if not n.startswith("."):
                files.append(os.path.relpath(os.path.join(root, n), package_dir))
    return {
        "name": spec.name,
        "version": str(spec.version),
        "id": _node_id(spec),
        "arch": _ARCH.get(str(spec.target.family), str(spec.target.family)),
        "prefix": str(prefix),
        "sh": os.path.join(deps[shell_dep], "bin", "sh"),
        "stage_dir": stage_dir,
        "source_dir": source_dir,
        "package_dir": package_dir,
        "jobs": spack.config.determine_number_of_jobs(parallel=True),
        # as in shpack: -j comes with MAKEFLAGS, and parallel(False) pins -j1
        "makejobs": [] if pkg.parallel else ["-j1"],
        "file_prefix_map": f"-ffile-prefix-map={stage_dir}=.",
        "debug_prefix_map": f"-fdebug-prefix-map={stage_dir}=.",
        "package_files": sorted(files),
        "deps": deps,
    }


def _build_env(spec, ctx: Dict[str, Any]) -> Dict[str, str]:
    """The environment shpack's builder gives a plan: nothing inherited from Spack or the
    user, only the variables listed in the file SPACK_STAR_BASE_ENV (``KEY=VALUE`` lines;
    shpack's ROOT, STORE, BASEPATH, TMPDIR, HOME, ...) plus what is computed per node."""
    env: Dict[str, str] = {}
    base_env = os.environ.get("SPACK_STAR_BASE_ENV")
    if base_env:
        with open(base_env, "r", encoding="utf-8") as f:
            for line in f:
                key, eq, value = line.rstrip("\n").partition("=")
                if eq:
                    env[key] = value
    basepath = env.get("BASEPATH", os.environ.get("PATH", ""))
    path = [os.path.join(ctx["prefix"], "bin")]
    path += [os.path.join(str(n.prefix), "bin") for n in reversed(_closure_order(spec))]
    include, link, pkgconfig = [], [], []
    for dep in _declared_deps(spec):
        p = str(dep.prefix)
        if os.path.isdir(os.path.join(p, "include")):
            include.append(os.path.join(p, "include"))
        for lib in (os.path.join(p, "lib64"), os.path.join(p, "lib")):
            if os.path.isdir(lib):
                link.append(lib)
                if os.path.isdir(os.path.join(lib, "pkgconfig")):
                    pkgconfig.append(os.path.join(lib, "pkgconfig"))
    env.update(
        {
            "PATH": ":".join(path) + ":" + basepath,
            "PREFIX": ctx["prefix"],
            "ARCH": ctx["arch"],
            "JOBS": str(ctx["jobs"]),
            "makejobs": " ".join(ctx["makejobs"]),
            "sh": ctx["sh"],
            "SHELL": ctx["sh"],
            "MAKEFLAGS": f"-j{ctx['jobs']} SHELL={ctx['sh']}",
            "MFLAGS": f"-j{ctx['jobs']}",
            "SOURCE_DATE_EPOCH": "0",
            "SHPACK_INCLUDE_DIRS": ":".join(include),
            "SHPACK_LINK_DIRS": ":".join(link),
            "SHPACK_RPATH_DIRS": ":".join(link),
            "PKG_CONFIG_PATH": ":".join(pkgconfig),
            "SHPACK_FILE_PREFIX_MAP": ctx["file_prefix_map"],
        }
    )
    return env


def _starlark_literal(value) -> str:
    """ctx as Starlark source: JSON is valid Starlark for these types."""
    return json.dumps(value, indent=4, sort_keys=False)


def _unpack(archive: str, into: str, env: Dict[str, str]) -> None:
    """shpack's unpack(): compressors piped into tar, all taken from the build's PATH."""
    a = shlex.quote(archive)
    if archive.endswith((".tar.gz", ".tgz")):
        cmd = f"gzip -dc {a} | tar -xf -"
    elif archive.endswith(".tar.bz2"):
        cmd = f"bzip2 -dc {a} | tar -xf -"
    elif archive.endswith((".tar.xz", ".tar.lzma")):
        cmd = f"unxz < {a} | tar -xf -"
    elif archive.endswith(".tar"):
        cmd = f"tar -xf {a}"
    else:
        cmd = f"cp {a} ."
    subprocess.run(["/bin/sh", "-ec", cmd], cwd=into, env=env, check=True)


def _stage(pkg, spec, env: Dict[str, str]) -> str:
    """Lay the sources out as shpack's do_stage does: every archive (the main source, then
    the resources) unpacked side by side in one stage directory, by the tools on the build's
    PATH -- under SPACK_STAR_STAGE_ROOT if set, so that build paths match shpack's."""
    root = os.environ.get("SPACK_STAR_STAGE_ROOT")
    stage_dir = os.path.join(root, _node_id(spec)) if root else os.path.join(pkg.stage.path, "s")
    shutil.rmtree(stage_dir, ignore_errors=True)
    mkdirp(stage_dir)
    if pkg.has_code:
        _unpack(pkg.stage.archive_file, stage_dir, env)
    for when, sha256, url, fname in pkg._star_resources:
        if when and not spec.satisfies(when):
            continue
        fetcher = spack.fetch_strategy.URLFetchStrategy(url=url, checksum=sha256)
        with spack.stage.stage_from_config(
            fetcher, config=spack.config.CONFIG, name=f"{spec.name}-resource-{sha256[:7]}"
        ) as stage:
            stage.fetch()
            stage.check()
            _unpack(stage.archive_file, stage_dir, env)
    return stage_dir


def _patch(pkg, spec, source_dir: str, env: Dict[str, str]) -> None:
    """shpack's do_patch: the recipe's patches, in order, with the patch on the build PATH
    (Spack's own patching went to its spack-src copy, which is not used)."""
    package_dir = os.path.dirname(pkg._star_file)
    for fname, level, when in pkg._star_patches:
        if when and not spec.satisfies(when):
            continue
        with open(os.path.join(package_dir, "patches", fname), "rb") as f:
            subprocess.run(
                ["/bin/sh", "-ec", f"patch -p{int(level)}"],
                stdin=f,
                cwd=source_dir,
                env=env,
                check=True,
            )


def _install(self, spec, prefix):
    # The environment depends on ctx only through prefix/sh/jobs/maps; stage with a
    # provisional one (no stage paths are in it), then build the final one.
    env = _build_env(spec, _ctx(self, spec, prefix, "", ""))
    stage_dir = _stage(self, spec, env)
    # the source directory is the first directory in the stage (shpack's do_stage)
    dirs = sorted(d for d in os.listdir(stage_dir) if os.path.isdir(os.path.join(stage_dir, d)))
    source_dir = os.path.join(stage_dir, dirs[0]) if dirs else stage_dir
    ctx = _ctx(self, spec, prefix, stage_dir, source_dir)
    env = _build_env(spec, ctx)
    _patch(self, spec, source_dir, env)
    patch_shebangs = os.environ.get("SPACK_STAR_PATCH_SHEBANGS")
    if patch_shebangs:
        subprocess.run([patch_shebangs, ctx["sh"], stage_dir], check=True)
    ctx_file = os.path.join(stage_dir, "..", f"{ctx['id']}.ctx.star")
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
    os.remove(ctx_file)
    mkdirp(str(prefix))
    executor = _Executor(ctx, source_dir, env)
    for phase in plan["phases"]:
        for action in phase["actions"]:
            executor.do(action)
    # as shpack's finalize: no libtool archives, no stage left behind
    for libdir in ("lib", "lib64"):
        for la in glob.glob(os.path.join(str(prefix), libdir, "*.la")):
            os.remove(la)
    shutil.rmtree(stage_dir, ignore_errors=True)


class _Executor:
    """Performs plan actions with the same semantics as shpack's sh renderer."""

    def __init__(self, ctx: Dict[str, Any], cwd: str, env: Dict[str, str]):
        self.ctx = ctx
        self.cwd = cwd
        self.env = env

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

    def _env(self, extra=None) -> Dict[str, str]:
        env = dict(self.env)
        env["PWD"] = self.cwd
        env.update(extra or {})
        return env

    def _run(self, a):
        cwd = self.path(a["cwd"]) if a.get("cwd") else self.cwd
        env = self._env(a.get("env"))
        argv = list(a["argv"])
        if os.sep not in argv[0]:
            exe = shutil.which(argv[0], path=env["PATH"])
            if not exe:
                raise StarError(f"{argv[0]}: not found on the build PATH")
            argv[0] = exe
        out = open(self.path(a["stdout"]), "wb") if a.get("stdout") else None
        try:
            subprocess.run(argv, cwd=cwd, env=env, stdout=out, check=True)
        finally:
            if out:
                out.close()

    def _sh(self, a):
        cwd = self.path(a["cwd"]) if a.get("cwd") else self.cwd
        subprocess.run([self.ctx["sh"], "-ec", a["script"]], cwd=cwd, env=self._env(), check=True)

    def _setenv(self, a):
        self.env[a["name"]] = a["value"]

    def _prepend_path(self, a):
        old = self.env.get(a["name"])
        self.env[a["name"]] = a["value"] + (":" + old if old else "")

    def _unsetenv(self, a):
        self.env.pop(a["name"], None)

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
    record = _record(packages_path, root, os.path.basename(pkg_dir))
    parts = []
    for path in [filename] + [os.path.join(root, m) for m in record["loads"]]:
        with open(path, "r", encoding="utf-8") as f:
            parts.append(f.read())
    return "\0".join(parts)
