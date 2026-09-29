# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Package recipes written in Starlark (``package.star``).

A ``package.star`` is a recipe in the format of shpack's recipes (see
``star/PROTOCOL.md`` in shpack): directives declare versions, dependencies and
patches, and phase functions return *actions* instead of performing them. The
file is written in the common subset of Starlark and Python, so shpack's ``star``
and Python (:mod:`spack.starlark_eval`) evaluate it to the same records:

* the directive record, from which this module builds an ordinary
  :class:`spack.package_base.PackageBase` subclass (so concretization sees
  versions, dependencies and patches as usual);
* at install time, the plan: the phases evaluated against a build context
  derived from the concrete spec, whose actions are executed here.

``resource()`` keeps shpack's semantics (unpacked flat into the stage, beside the
source) by fetching in the install step rather than through Spack's resources,
so ``spack mirror`` does not see them yet.
"""

import glob
import hashlib
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
import spack.starlark_eval
import spack.util.naming as nm
from spack.error import SpackError
from spack.util.filesystem import copy_tree, filter_file, mkdirp

STAR_FILE_NAME = "package.star"

#: shpack's arch names, by target family
_ARCH = {"x86_64": "amd64", "aarch64": "aarch64"}

#: The evaluator shpack names in its package text (star --version)
STAR_VERSION = "star 1.0"

#: The operating system of every Starlark package: what they build does not depend on the
#: host's, and shpack records it (platform_os) so that both compute the same hashes
STAR_OS = "shpack"


class StarError(SpackError):
    """Error evaluating or executing a Starlark package recipe."""


_records: Dict[str, Any] = {}


def _record(packages_path: str, root: str, name: str) -> Any:
    key = os.path.join(packages_path, name)
    if key not in _records:
        try:
            _records[key] = spack.starlark_eval.recipe(packages_path, root, name)
        except spack.starlark_eval.StarlarkError as e:
            raise StarError(f"{name}: {e}") from e
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


def _register_os() -> None:
    """Make STAR_OS an operating system the host platform builds for (Spack otherwise
    knows only the host's, and those named on the command line)."""
    import spack.operating_systems
    import spack.platforms

    platform = spack.platforms.host()
    if STAR_OS not in platform.operating_sys:
        platform.add_operating_system(
            STAR_OS, spack.operating_systems.OperatingSystem(STAR_OS, "")
        )


def make_package_class(repo, pkg_name: str, filename: str) -> type:
    """Build the package class for ``pkg_name`` from its ``package.star``."""
    _register_os()
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
        # shpack builds several versions of musl, gawk, xz, ... into one DAG; tagged
        # build-tools, Spack lets a package appear once per version among build deps.
        "tags": ["build-tools"],
        # the recipe's docstring and globals are what a package keeps as class attributes
        "__doc__": record["description"],
        "parallel": record["parallel"],
        "_star_file": filename,
        "_star_root": root,
        "_star_packages": packages_path,
        "install": _install,
    }
    if record["homepage"]:
        attrs["homepage"] = record["homepage"]

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
    kaem = _kaem_steps(os.path.join(packages_path, pkg_name))
    for d in record["directives"]:
        kind = d["directive"]
        when = d.get("when")
        if kind == "version":
            kwargs = {}
            if d["version"] in kaem:
                # a kaem step unpacks its own archives (from DISTFILES): Spack would do
                # it with the tar on the build PATH, which may be the kaem phase's
                kwargs["expand"] = False
            if d["sha256"]:
                kwargs["sha256"] = d["sha256"]
                has_code = True
            if d["url"]:
                kwargs["url"] = d["url"]
                first_url = first_url or d["url"]
            spack.directives.version(d["version"], **kwargs)
        elif kind == "depends_on":
            # To Spack every edge is a build edge: the bootstrap links several libcs
            # (musl 1.1.24 and 1.2.5, glibc-boot and glibc) and shells into what the
            # concretizer would make one unification set, and it cannot duplicate
            # link dependencies that way. The recipe's own types (_star_deps) decide
            # what the build sees: PATH, the wrapper's -I/-L/rpath, PKG_CONFIG_PATH.
            spack.directives.depends_on(
                _resolve(packages_path, root, d["spec"]), when=when, type="build"
            )
            star_deps.append((when, d["spec"], tuple(d["type"])))
        elif kind == "patch":
            spack.directives.patch(os.path.join("patches", d["file"]), level=d["level"], when=when)
            star_patches.append((d["file"], d["level"], when))
        elif kind == "license":
            spack.directives.license(d["license"], when=when)
        elif kind == "build_system":
            pass  # the plan carries it
        elif kind == "resource":
            resources.append((when, d["sha256"], d["url"], d["fname"]))
    if first_url:
        attrs["url"] = first_url
    if not has_code:
        attrs["has_code"] = False
    attrs["_star_resources"] = resources
    attrs["_star_deps"] = star_deps
    attrs["_star_patches"] = star_patches

    # what a recipe builds does not depend on the host's operating system
    spack.directives.requires(f"os={STAR_OS}")

    base = spack.builder.Package
    cls = type(base)(nm.pkg_name_to_class_name(pkg_name), (base,), attrs)  # type: ignore[misc]
    setattr(module, cls.__name__, cls)
    return cls


# --------------------------------------------------------------------- install


def _declared_edges(node) -> List:
    """``node``'s dependencies with their types, in the order its recipe declares them
    (shpack's order), restricted to its version; externals and non-Starlark packages have
    none."""
    cls = spack.repo.PATH.get_pkg_class(node.fullname)
    if node.external or not hasattr(cls, "_star_deps"):
        return []
    out: List = []
    for when, dep_spec, deptypes in cls._star_deps:
        if when and not node.satisfies(when):
            continue
        name = dep_spec.partition("@")[0]
        out.extend((dep, deptypes) for dep in node.dependencies(name=name))
    return out


def _declared_deps(node) -> List:
    return [dep for dep, _ in _declared_edges(node)]


def _exec(node) -> Dict[str, Any]:
    """What a dependent may run through ``node`` (Spack's RUNTIME_EXECUTABLE): its run
    dependencies, and theirs and its link dependencies', by DAG hash."""
    out: Dict[str, Any] = {}
    for dep, deptypes in _declared_edges(node):
        if "run" in deptypes:
            out[dep.dag_hash()] = dep
        if "run" in deptypes or "link" in deptypes:
            out.update(_exec(dep))
    return out


def _runnable(node) -> Dict[str, Any]:
    """Whose ``bin`` a build of ``node`` has on PATH, as in Spack's effective_deptypes:
    its build (and test) dependencies, and what each of those runs."""
    out: Dict[str, Any] = {}
    for dep, deptypes in _declared_edges(node):
        if "build" in deptypes or "test" in deptypes:
            out[dep.dag_hash()] = dep
            out.update(_exec(dep))
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
    runnable = _runnable(spec)
    path += [
        os.path.join(str(n.prefix), "bin")
        for n in reversed(_closure_order(spec))
        if n.dag_hash() in runnable
    ]
    include, link, pkgconfig = [], [], []
    for dep, deptypes in _declared_edges(spec):
        if "link" not in deptypes:
            continue
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


_ARCH_DIR = {"aarch64": "AArch64", "amd64": "AMD64"}


def _kaem_distfiles(pkg, spec, into: str) -> None:
    """The step's sources as the kaem phase expects them: archives, not unpacked, under
    their bare names in one directory (its DISTFILES)."""
    mkdirp(into)
    if pkg.has_code:
        version = [d for d in _record_of(pkg)["directives"]
                   if d["directive"] == "version" and d["version"] == str(spec.version)][0]
        fname = version["fname"] or os.path.basename(version["url"])
        shutil.copyfile(pkg.stage.archive_file, os.path.join(into, fname))
    for when, sha256, url, fname in pkg._star_resources:
        if when and not spec.satisfies(when):
            continue
        fetcher = spack.fetch_strategy.URLFetchStrategy(url=url, checksum=sha256)
        with spack.stage.stage_from_config(
            fetcher, config=spack.config.CONFIG, name=f"{spec.name}-resource-{sha256[:7]}"
        ) as stage:
            stage.fetch()
            stage.check()
            shutil.copyfile(stage.archive_file, os.path.join(into, fname))


def _record_of(pkg) -> Any:
    return _record(pkg._star_packages, pkg._star_root, os.path.basename(os.path.dirname(pkg._star_file)))


def _seed_path(seed: str, tree: str) -> List[str]:
    """shpack/bootstrap/seed.path: what the seed puts on the PATH of the steps after it."""
    with open(os.path.join(tree, "shpack", "bootstrap", "seed.path"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("PATH="):
                return line.strip()[len("PATH="):].replace("${SEED}", seed).split(":")
    raise StarError("seed.path has no PATH= line")


def _kaem_install(pkg, spec, prefix, step: str) -> None:
    """Build a kaem-phase package as shpack's kaem phase does: the seed by the stage0 seed
    itself (start.kaem, COMMAND=seed), any other step by its kaem.run under the kaem phase's
    environment contract (shpack/bootstrap/README.md), into the unhashed prefix
    <store>/<name>-<version> that the kaem phase uses too."""
    tree = os.path.dirname(pkg._star_root)
    arch = _ARCH.get(str(spec.target.family), str(spec.target.family))
    # self.stage needs getpwuid(), which a build sandbox may deny; phases run in the stage
    stage = os.path.dirname(os.getcwd())
    distfiles = os.path.join(stage, "distfiles")
    build = os.path.join(stage, "build")
    _kaem_distfiles(pkg, spec, distfiles)
    mkdirp(os.path.join(build, "home"))
    if step == "seed":
        if os.path.basename(str(prefix)) != _node_id(spec):
            raise StarError(f"{spec.name}: the seed installs at <store>/{_node_id(spec)}, "
                            f"not {prefix} (install_tree projections)")
        # the seed writes into its own tree (seed/), so it runs on a copy
        src = os.path.join(stage, "tree")
        shutil.rmtree(src, ignore_errors=True)
        skip = [p[1:] for p in _kaem_steps(os.path.dirname(pkg._star_file))[str(spec.version)]
                if p.startswith("!")]
        for d in ("seed", "vendor", os.path.join("shpack", "bootstrap")):
            shutil.copytree(os.path.join(tree, d), os.path.join(src, d), symlinks=True)
        for d in skip:   # stale outputs of a run in the tree: the seed makes its own
            shutil.rmtree(os.path.join(src, d), ignore_errors=True)
            mkdirp(os.path.join(src, d))
        with open(os.path.join(src, "shpack.conf"), "w", encoding="utf-8") as f:
            f.write(f"STORE={os.path.dirname(str(prefix))}\nBUILDDIR={build}\n"
                    f"DISTFILES={distfiles}\nCOMMAND=seed\n")
        seed = os.path.join("bootstrap-seeds", "POSIX", _ARCH_DIR[arch], "kaem-optional-seed")
        subprocess.run([seed, f"kaem.{arch}"], cwd=os.path.join(src, "seed"), env={}, check=True)
        # the stage0 seed exits 0 even when the chain aborts
        if not os.path.exists(os.path.join(str(prefix), "bin", "tcc")):
            raise StarError(f"{spec.name}: the seed chain failed")
        return
    deps = _declared_deps(spec)
    seeds = [d for d in deps if _kaem_steps(os.path.dirname(
        spack.repo.PATH.get_pkg_class(d.fullname)._star_file)).get(str(d.version), [None])[0] == "seed"]
    if len(seeds) != 1:
        raise StarError(f"{spec.name}: a kaem step depends on the seed (tcc)")
    seed = str(seeds[0].prefix)
    boot = os.path.join(tree, "shpack", "bootstrap")
    bindir = os.path.join(str(prefix), "bin")
    # start.kaem's PATH at this step: its own bin, the steps before it newest first, the seed
    path = [bindir] + [os.path.join(str(d.prefix), "bin") for d in reversed(deps) if d not in seeds]
    env = {
        "ROOT": tree, "ARCH": arch, "ARCH_DIR": _ARCH_DIR[arch],
        "STORE": os.path.dirname(str(prefix)), "DISTFILES": distfiles,
        "BUILDDIR": build, "TMPDIR": build, "HOME": os.path.join(build, "home"), "TERM": "dumb",
        "SEEDDIR": os.path.join(tree, "seed"), "BOOT": boot,
        "MESR": os.path.join(tree, "vendor", "mes-replacement"),
        "LIBC_PREFIX": seed, "LIBDIR": os.path.join(seed, "lib"),
        "INCDIR": os.path.join(seed, "include"), "pkg": step, "PKG": os.path.join(boot, step),
        "PREFIX": str(prefix), "BINDIR": bindir, "PATH": ":".join(path + _seed_path(seed, tree)),
    }
    mkdirp(bindir)
    kaem = os.path.join(seed, "mescc-tools-1.7.0", "bin", "kaem")
    subprocess.run([kaem, "--verbose", "--strict", "--file", os.path.join(boot, step, "kaem.run")],
                   cwd=tree, env=env, check=True)


def _normalize_modes(prefix: str) -> None:
    """shpack's finalize (chmod -R u=rwX,go=rX): directories 755, files 644, or 755 if any
    execute bit is set."""
    for root, dirs, files in os.walk(prefix):
        os.chmod(root, 0o755)
        for n in files:
            p = os.path.join(root, n)
            if not os.path.islink(p):
                os.chmod(p, 0o755 if os.stat(p).st_mode & 0o111 else 0o644)


def _install(self, spec, prefix):
    step = _kaem_steps(os.path.dirname(self._star_file)).get(str(spec.version))
    if step:
        _kaem_install(self, spec, prefix, step[0])
        # as shpack registers a kaem-phase prefix
        _normalize_modes(str(prefix))
        return
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
    plan = spack.starlark_eval.plan(
        self._star_packages,
        self._star_root,
        os.path.basename(os.path.dirname(self._star_file)),
        ctx,
    )
    mkdirp(str(prefix))
    if os.environ.get("SPACK_STAR_KEEP_STAGE"):
        with open(os.path.join(stage_dir, "..", f"{ctx['id']}.env"), "w", encoding="utf-8") as f:
            f.writelines(f"{k}={v}\n" for k, v in sorted(env.items()))
    executor = _Executor(ctx, source_dir, env)
    for phase in plan["phases"]:
        for action in phase["actions"]:
            executor.do(action)
    # as shpack's finalize: no libtool archives, no stage left behind
    for libdir in ("lib", "lib64"):
        for la in glob.glob(os.path.join(str(prefix), libdir, "*.la")):
            os.remove(la)
    if not os.environ.get("SPACK_STAR_KEEP_STAGE"):
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


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _package_files(pkg_dir: str, rel: str = "") -> List:
    """shpack's host.files: (path, sha256) of every file in the package directory, depth
    first, names sorted bytewise, dotfiles and dangling symlinks skipped."""
    out: List = []
    here = os.path.join(pkg_dir, rel) if rel else pkg_dir
    for name in sorted(os.listdir(here), key=lambda n: n.encode()):
        if name.startswith("."):
            continue
        path = os.path.join(here, name)
        r = f"{rel}/{name}" if rel else name
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            out.extend(_package_files(pkg_dir, r))
        else:
            out.append((r, _sha256_file(path)))
    return out


def _kaem_steps(pkg_dir: str) -> Dict[str, List[str]]:
    """VERSION -> [STEP, INPUT...] from the recipe's kaem-steps: the versions shpack's kaem
    phase builds, by shpack/bootstrap/STEP/kaem.run (or, for STEP "seed", the stage0 seed
    up to COMMAND=seed), and the tree paths the step reads."""
    out: Dict[str, List[str]] = {}
    path = os.path.join(pkg_dir, "kaem-steps")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                if fields and not fields[0].startswith("#"):
                    out[fields[0]] = fields[1:]
    return out


def source_hash(spec, filename: str) -> str:
    """What Spack's package hash sees of a Starlark recipe: shpack's package text
    (package_text in its lib/concretize.star), everything that determines the build but
    the dependencies -- the sources of this version and its resources, every file in the
    package directory, the evaluator, and every module the recipe loads -- so that shpack
    and Spack compute the same DAG hashes."""
    pkg_dir = os.path.dirname(filename)
    packages_path = os.path.dirname(pkg_dir)
    root = os.path.dirname(packages_path)
    record = _record(packages_path, root, os.path.basename(pkg_dir))
    version = str(spec.version)
    family = str(spec.target.family)
    out = [f"package {spec.name}", f"version {version}", f"arch {_ARCH.get(family, family)}"]
    for d in record["directives"]:
        if d["directive"] == "version" and d["version"] == version and d["sha256"]:
            out.append(f"source {d['sha256']} {d['fname'] or '-'}")
    for d in record["directives"]:
        if d["directive"] == "resource" and (not d["when"] or spec.satisfies(d["when"])):
            out.append(f"source {d['sha256']} {d['fname']}")
    out.extend(f"file {sha} {path}" for path, sha in _package_files(pkg_dir))
    # a kaem step also depends on the tree it runs, by content
    # ("!PATH" leaves out what is under PATH: the seed's own build outputs)
    tree = os.path.dirname(root)
    inputs = _kaem_steps(pkg_dir).get(version, [])[1:]
    skip = tuple(p[1:] + "/" for p in inputs if p.startswith("!"))
    for path in inputs:
        if path.startswith("!"):
            continue
        full = os.path.join(tree, path)
        if not os.path.isdir(full):
            out.append(f"input {_sha256_file(full)} {path}")
            continue
        for rel, sha in _package_files(full):
            if not f"{path}/{rel}".startswith(skip):
                out.append(f"input {sha} {path}/{rel}")
    out.append(f"evaluator {STAR_VERSION}")
    out.extend(f"load {_sha256_file(os.path.join(root, m))} {m}" for m in record["loads"])
    return "".join(line + "\n" for line in out)
