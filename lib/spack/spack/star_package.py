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

A repository loads a ``package.star`` as a package module (``spack.repo._StarLoader``),
which calls :func:`define_package`: the package class derives from :class:`StarPackage`
and holds the recipe's record. The recipe as data (its record, its package text, the
input of the package hash) is :mod:`spack.star_recipe`.

Spack fetches every archive, versions and ``resource()`` alike, unexpanded; staging
copies into the stage all the build reads, before a build sandbox applies; the build
unpacks, patches and runs the plan with the tools of its own PATH (star/PROTOCOL.md in
shpack, Hosts).
"""

import fnmatch
import glob
import os
import shutil
import subprocess
import types
from typing import Any, Dict, List, Optional, Set, Tuple

import spack.builder
import spack.config
import spack.directives
import spack.directives_meta
import spack.operating_systems
import spack.package_base
import spack.platforms
import spack.repo
import spack.stage
import spack.star_recipe as star_recipe
import spack.starlark_eval
import spack.util.naming as nm
from spack.util.filesystem import copy_tree, filter_file, mkdirp


def _resolve(packages_path: str, root: str, spec: str) -> str:
    """shpack's resolution, spelled for Spack: ``name@version`` pins that exact version,
    and a bare name means the first version its recipe declares (a recipe always beats
    an external); names without a recipe stay bare and resolve to an external."""
    name, at, version = spec.partition("@")
    if at:
        return f"{name}@={version}"
    if not os.path.exists(os.path.join(packages_path, name, spack.repo.star_file_name)):
        return spec
    for d in star_recipe.record(packages_path, root, name)["directives"]:
        if d["directive"] == "version":
            return f"{name}@={d['version']}"
    return spec


def _register_os() -> None:
    """Make star_recipe.STAR_OS an operating system the host platform builds for (Spack
    otherwise knows only the host's, and those named on the command line)."""
    platform = spack.platforms.host()
    if star_recipe.STAR_OS not in platform.operating_sys:
        platform.add_operating_system(
            star_recipe.STAR_OS, spack.operating_systems.OperatingSystem(star_recipe.STAR_OS, "")
        )


def define_package(module: types.ModuleType) -> None:
    """Define the package class of a ``package.star`` in its package module, which the
    repository's import machinery created for it (``spack.repo._StarLoader``), as a
    ``package.py`` defines its own: a :class:`StarPackage` holding the recipe's record,
    with the recipe's directives."""
    _register_os()
    pkg_name = module.__loader__.package_name  # type: ignore[union-attr]
    assert module.__file__ is not None
    pkg_dir = os.path.dirname(module.__file__)
    packages_path = os.path.dirname(pkg_dir)
    root = os.path.dirname(packages_path)  # load("//...") resolves here
    record = star_recipe.record(packages_path, root, os.path.basename(pkg_dir))

    # Refuse what Spack cannot express before a single directive is queued: queued
    # directives go to the next package class created, whichever that is.
    for d in record["directives"]:
        if d["directive"] == "depends_on" and d["spec"].partition("@")[0] == pkg_name:
            raise star_recipe.StarError(f"{pkg_name} depends on itself ({d['spec']})")

    attrs: Dict[str, Any] = {
        "__module__": module.__name__,
        # the recipe's docstring and globals are what a package keeps as class attributes
        "__doc__": record["description"],
        "parallel": record["parallel"],
        "star_record": record,
        "star_dir": pkg_dir,
        "star_kaem": star_recipe.kaem_steps(pkg_dir),
    }
    if record["homepage"]:
        attrs["homepage"] = record["homepage"]

    # Directives queue up and are consumed by the next package class created,
    # so every one of them is called right before the class below.
    try:
        _queue_directives(record, packages_path, root, attrs)
        cls = type(StarPackage)(  # type: ignore[misc]
            nm.pkg_name_to_class_name(pkg_name), (StarPackage,), attrs
        )
    except BaseException:
        spack.directives_meta.DirectiveMeta._directives_to_be_executed.clear()
        raise
    setattr(module, cls.__name__, cls)


def _queue_directives(record, packages_path: str, root: str, attrs: Dict[str, Any]) -> None:
    """The recipe's directives as Spack's, and the class attributes they imply."""
    has_code = False
    for d in record["directives"]:
        kind = d["directive"]
        when = d.get("when")
        if kind == "version":
            # Spack fetches and checks the archives; the build unpacks them, with the
            # tools on its PATH (the protocol's staging), or a kaem step by itself
            kwargs: Dict[str, Any] = {"expand": False}
            if d["sha256"]:
                kwargs["sha256"] = d["sha256"]
                has_code = True
            if d["url"]:
                kwargs["url"] = d["url"]
                attrs.setdefault("url", d["url"])
            spack.directives.version(d["version"], **kwargs)
        elif kind == "depends_on":
            # To Spack every edge is a build edge: the bootstrap links several libcs
            # (musl 1.1.24 and 1.2.5, glibc-boot and glibc) and shells into what the
            # concretizer would make one unification set, and it cannot duplicate
            # link dependencies that way. The recipe's own types (declared_edges) decide
            # what the build sees: PATH, the wrapper's -I/-L/rpath, PKG_CONFIG_PATH.
            spack.directives.depends_on(
                _resolve(packages_path, root, d["spec"]), when=when, type="build"
            )
        elif kind == "patch":
            spack.directives.patch(os.path.join("patches", d["file"]), level=d["level"], when=when)
        elif kind == "license":
            spack.directives.license(d["license"], when=when)
        elif kind == "resource":
            spack.directives.resource(
                name=d["fname"], url=d["url"], sha256=d["sha256"], expand=False, when=when
            )
        # build_system: the plan carries it
    if not has_code:
        attrs["has_code"] = False


class StarPackage(spack.builder.Package):
    """The base class of the package class of a ``package.star`` (:func:`define_package`
    derives one per recipe). Staging copies in what the build reads; installing runs the
    recipe's plan, or, for a version the kaem phase builds, that kaem step."""

    build_system_class = "StarPackage"

    #: shpack builds several versions of musl, gawk, xz, ... into one DAG; tagged
    #: build-tools, Spack lets a package appear once per version among build deps.
    tags = ["build-tools"]

    #: The recipe's directive record (``star recipe``, :func:`spack.starlark_eval.recipe`)
    star_record: Dict[str, Any] = {"directives": [], "loads": []}
    #: The recipe's package directory
    star_dir = ""
    #: VERSION -> [STEP, INPUT...] from the recipe's kaem-steps (star_recipe.kaem_steps)
    star_kaem: Dict[str, List[str]] = {}

    # what a recipe builds does not depend on the host's operating system
    spack.directives.requires(f"os={star_recipe.STAR_OS}")

    @classmethod
    def star_directives(cls, kind: str) -> List[Dict[str, Any]]:
        """The recipe's directives of one kind, in recipe order."""
        return [d for d in cls.star_record["directives"] if d["directive"] == kind]

    @classmethod
    def package_text(cls, spec) -> str:
        """What Spack's package hash sees of the recipe (star_recipe.package_text)."""
        return star_recipe.package_text(spec, cls.star_dir, cls.star_record)

    @property
    def star_root(self) -> str:
        """The repository root, where ``load("//...")`` resolves."""
        return os.path.dirname(os.path.dirname(self.star_dir))

    def _applies(self, d: Dict[str, Any]) -> bool:
        return not d.get("when") or self.spec.satisfies(d["when"])

    @property
    def kaem_step(self) -> Optional[List[str]]:
        """[STEP, INPUT...] if the kaem phase builds this version."""
        return self.star_kaem.get(str(self.spec.version))

    @property
    def is_seed(self) -> bool:
        step = self.kaem_step
        return step is not None and step[:1] == ["seed"]

    def declared_edges(self) -> List[Tuple[Any, Tuple[str, ...]]]:
        """The dependencies with their types, in the order the recipe declares them
        (shpack's order), restricted to this version; an external has none."""
        if self.spec.external:
            return []
        out: List[Tuple[Any, Tuple[str, ...]]] = []
        for d in self.star_directives("depends_on"):
            if self._applies(d):
                name = d["spec"].partition("@")[0]
                out.extend((dep, tuple(d["type"])) for dep in self.spec.dependencies(name=name))
        return out

    # ----------------------------------------------------------------- staging

    @property
    def _inputs(self) -> str:
        """Where staging put what the build reads (``do_stage``): the stage's own copy."""
        return os.path.join(self.stage.path, "shpack")

    @property
    def _tree(self) -> str:
        """The build's copy of the tree (the repository root's parent): the recipe
        directory, the modules it loads, a kaem step's inputs, and seed.path."""
        return os.path.join(self._inputs, "tree")

    def _in_tree(self, path: str) -> str:
        """The build's copy of ``path``, a path in the tree."""
        return os.path.join(self._tree, os.path.relpath(path, os.path.dirname(self.star_root)))

    def do_stage(self, mirror_only=False):
        """Spack's staging (fetch and checksum; the archives stay packed), then a copy in
        the stage of all that the build reads, before any build sandbox applies: the
        archives under their names, the recipe directory and the modules it loads, and for
        a kaem step the tree paths it declares. These are what its package hash covers."""
        super().do_stage(mirror_only)
        spec = self.spec
        out = self._inputs
        shutil.rmtree(out, ignore_errors=True)
        distfiles = os.path.join(out, "distfiles")
        mkdirp(distfiles)
        sources = []
        if self.has_code:
            version = next(
                d for d in self.star_directives("version") if d["version"] == str(spec.version)
            )
            fname = version["fname"] or os.path.basename(version["url"])
            shutil.copyfile(self.stage.archive_file, os.path.join(distfiles, fname))
            sources.append(fname)
        archives = {
            s.resource.name: s.archive_file
            for s in self.stage
            if isinstance(s, spack.stage.ResourceStage)
        }
        for d in self.star_directives("resource"):
            if self._applies(d):
                shutil.copyfile(archives[d["fname"]], os.path.join(distfiles, d["fname"]))
                sources.append(d["fname"])
        with open(os.path.join(out, "sources"), "w", encoding="utf-8") as f:
            f.writelines(f"{s}\n" for s in sources)
        src_tree = os.path.dirname(self.star_root)
        paths = [self.star_dir, os.path.join(self.star_root, "bootstrap", "seed.path")]
        paths += [os.path.join(self.star_root, m) for m in self.star_record["loads"]]
        inputs = (self.kaem_step or [""])[1:]
        paths += [os.path.join(src_tree, p) for p in inputs if not p.startswith("!")]
        for p in paths:
            _copy_input(p, self._in_tree(p))
        for p in inputs:  # "!PATH": left out, but the directory is there (stage0's outputs)
            if p.startswith("!"):
                shutil.rmtree(os.path.join(self._tree, p[1:]), ignore_errors=True)
                mkdirp(os.path.join(self._tree, p[1:]))
        # the build may need any package class of its DAG, and cannot read the repo then
        for node in spec.traverse():
            spack.repo.PATH.get_pkg_class(node.fullname)

    def do_patch(self):
        """Staging only: the build applies the recipe's patches after unpacking (the
        protocol's staging), so Spack's patch step has nothing to patch."""
        self.do_stage()

    # ----------------------------------------------------------------- install

    def install(self, spec, prefix):
        step = self.kaem_step
        if step:
            self._kaem_install(str(prefix), step[0])
        else:
            self._plan_install(str(prefix))
        # shpack's finalize (and how it registers a kaem-phase prefix)
        _normalize_modes(str(prefix))

    def _seed_path_file(self) -> str:
        return self._in_tree(os.path.join(self.star_root, "bootstrap", "seed.path"))

    def _kaem_install(self, prefix: str, step: str) -> None:
        """Build a kaem-phase package as shpack's kaem phase does: the seed by the stage0
        seed itself (start.kaem, COMMAND=seed), any other step by its kaem.run under the
        kaem phase's environment contract (shpack/bootstrap/README.md), into the unhashed
        prefix <store>/<name>-<version> that the kaem phase uses too."""
        spec = self.spec
        tree = self._tree
        arch = star_recipe.arch(spec)
        distfiles = os.path.join(self._inputs, "distfiles")
        build = os.path.join(self._inputs, "build")
        mkdirp(os.path.join(build, "home"))
        if step == "seed":
            if os.path.basename(prefix) != _node_id(spec):
                raise star_recipe.StarError(
                    f"{spec.name}: the seed installs at <store>/{_node_id(spec)}, "
                    f"not {prefix} (install_tree projections)"
                )
            with open(os.path.join(tree, "shpack.conf"), "w", encoding="utf-8") as f:
                f.write(
                    f"STORE={os.path.dirname(prefix)}\nBUILDDIR={build}\n"
                    f"DISTFILES={distfiles}\nCOMMAND=seed\n"
                )
            seed = os.path.join("bootstrap-seeds", "POSIX", _ARCH_DIR[arch], "kaem-optional-seed")
            subprocess.run(
                [seed, f"kaem.{arch}"], cwd=os.path.join(tree, "seed"), env={}, check=True
            )
            # the stage0 seed exits 0 even when the chain aborts
            if not os.path.exists(os.path.join(prefix, "bin", "tcc")):
                raise star_recipe.StarError(f"{spec.name}: the seed chain failed")
            return
        seed = str([d for d in _deps(spec) if _is_seed(d)][0].prefix)
        boot = self._in_tree(os.path.join(self.star_root, "bootstrap"))
        bindir = os.path.join(prefix, "bin")
        env = {
            "ROOT": tree,
            "ARCH": arch,
            "ARCH_DIR": _ARCH_DIR[arch],
            "STORE": os.path.dirname(prefix),
            "DISTFILES": distfiles,
            "BUILDDIR": build,
            "TMPDIR": build,
            "HOME": os.path.join(build, "home"),
            "TERM": "dumb",
            "SEEDDIR": os.path.join(tree, "seed"),
            "BOOT": boot,
            "MESR": os.path.join(tree, "vendor", "mes-replacement"),
            "LIBC_PREFIX": seed,
            "LIBDIR": os.path.join(seed, "lib"),
            "INCDIR": os.path.join(seed, "include"),
            "pkg": step,
            "PKG": os.path.join(boot, step),
            "PREFIX": prefix,
            "BINDIR": bindir,
            "PATH": ":".join(_kaem_path(spec, bindir, self._seed_path_file())),
        }
        mkdirp(bindir)
        kaem = os.path.join(seed, "mescc-tools-1.7.0", "bin", "kaem")
        subprocess.run(
            [kaem, "--verbose", "--strict", "--file", os.path.join(boot, step, "kaem.run")],
            cwd=tree,
            env=env,
            check=True,
        )

    def _plan_install(self, prefix: str) -> None:
        spec = self.spec
        stage_dir = os.path.join(self._inputs, "stage")
        scratch = os.path.join(self._inputs, "tmp")
        mkdirp(stage_dir, os.path.join(scratch, "home"))
        closure = _closure_order(spec)
        ctx = self._ctx(prefix, closure, stage_dir)
        env = self._build_env(ctx, closure, scratch)
        self._stage_sources(stage_dir, env)
        # the source directory is the first directory in the stage (shpack's do_stage)
        dirs = sorted(
            d for d in os.listdir(stage_dir) if os.path.isdir(os.path.join(stage_dir, d))
        )
        source_dir = os.path.join(stage_dir, dirs[0]) if dirs else stage_dir
        ctx["source_dir"] = source_dir
        self._patch(ctx["package_dir"], source_dir, env)
        # #! lines in the whole stage point at the build shell (there is no /bin/sh)
        subprocess.run([_tool("patch-shebangs", env), ctx["sh"], stage_dir], env=env, check=True)
        plan = spack.starlark_eval.plan(
            os.path.dirname(ctx["package_dir"]), self._in_tree(self.star_root), spec.name, ctx
        )
        mkdirp(prefix)
        # the environment the plan runs in, for the record (shpack keeps it as spec/<id>/env)
        with open(os.path.join(self._inputs, "env"), "w", encoding="utf-8") as f:
            f.writelines(f"{k}={v}\n" for k, v in sorted(env.items()))
        executor = _Executor(ctx, source_dir, env)
        for phase in plan["phases"]:
            for action in phase["actions"]:
                executor.do(action)
        # as shpack's finalize: no libtool archives
        for libdir in ("lib", "lib64"):
            for la in glob.glob(os.path.join(prefix, libdir, "*.la")):
                os.remove(la)

    def _ctx(self, prefix: str, closure: List, stage_dir: str) -> Dict[str, Any]:
        """The build's ctx (star/PROTOCOL.md); staging fills in source_dir."""
        spec = self.spec
        package_dir = self._in_tree(self.star_dir)
        direct = _deps(spec)
        # name -> prefix over the direct deps, then the closure sorted by id: prefix_of
        deps: Dict[str, str] = {}
        for dep in direct + sorted(closure, key=_node_id):
            deps.setdefault(dep.name, str(dep.prefix))
        names = [d.name for d in direct]
        shell_dep = "dash" if "dash" in names else "dash-boot" if "dash-boot" in names else None
        if shell_dep is None:
            raise star_recipe.StarError(f"{spec.name} declares no shell dependency (dash)")
        return {
            "name": spec.name,
            "version": str(spec.version),
            "id": _node_id(spec),
            "arch": star_recipe.arch(spec),
            "prefix": prefix,
            "sh": os.path.join(deps[shell_dep], "bin", "sh"),
            "stage_dir": stage_dir,
            "source_dir": "",
            "package_dir": package_dir,
            "jobs": spack.config.determine_number_of_jobs(parallel=True),
            # as in shpack: -j comes with MAKEFLAGS, and parallel(False) pins -j1
            "makejobs": [] if self.parallel else ["-j1"],
            "file_prefix_map": f"-ffile-prefix-map={stage_dir}=.",
            "debug_prefix_map": f"-fdebug-prefix-map={stage_dir}=.",
            "package_files": star_recipe.walk_files(package_dir),
            "deps": deps,
        }

    def _base(self):
        """The base PATH and shell of a shell-phase build: the kaem phase's PATH and shell
        as it hands over to shpack (BASEPATH, CONFIG_SHELL), i.e. those of its last step,
        the kaem-phase node in the DAG that has all the others below it."""
        kaem = [n for n in self.spec.traverse(root=False) if _is_kaem(n) and not _is_seed(n)]
        if not kaem:
            raise star_recipe.StarError(
                f"{self.spec.name}: no kaem-phase package in its DAG (depends_on dash-boot)"
            )
        last = max(kaem, key=lambda n: len(list(n.traverse())))
        bindir = os.path.join(str(last.prefix), "bin")
        return _kaem_path(last, bindir, self._seed_path_file()), os.path.join(bindir, "sh")

    def _build_env(self, ctx: Dict[str, Any], closure: List, scratch: str) -> Dict[str, str]:
        """The environment of a plan (star/PROTOCOL.md, Hosts): nothing inherited from
        Spack or the user."""
        basepath, config_shell = self._base()
        path = [os.path.join(ctx["prefix"], "bin")]
        runnable = _runnable(self.spec)
        path += [
            os.path.join(str(n.prefix), "bin")
            for n in reversed(closure)
            if n.dag_hash() in runnable
        ]
        include, link, pkgconfig = [], [], []
        for dep, deptypes in self.declared_edges():
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
        return {
            "PATH": ":".join(path + basepath),
            "CONFIG_SHELL": config_shell,
            "HOME": os.path.join(scratch, "home"),
            "TMPDIR": scratch,
            "TERM": "dumb",
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

    def _stage_sources(self, stage_dir: str, env: Dict[str, str]) -> None:
        """shpack's do_stage: every archive (the main source, then the resources) unpacked
        side by side in one directory."""
        distfiles = os.path.join(self._inputs, "distfiles")
        with open(os.path.join(self._inputs, "sources"), encoding="utf-8") as f:
            for fname in f.read().split():
                _unpack(os.path.join(distfiles, fname), stage_dir, env)

    def _patch(self, package_dir: str, source_dir: str, env: Dict[str, str]) -> None:
        """shpack's do_patch: the recipe's patches, in order, with the patch on the build
        PATH."""
        for d in self.star_directives("patch"):
            if not self._applies(d):
                continue
            with open(os.path.join(package_dir, "patches", d["file"]), "rb") as f:
                subprocess.run(
                    [_tool("patch", env), f"-p{int(d['level'])}"],
                    stdin=f,
                    cwd=source_dir,
                    env=env,
                    check=True,
                )


# ---------------------------------------------------------------- the DAG, by node


def _edges(node) -> List[Tuple[Any, Tuple[str, ...]]]:
    """``node``'s declared dependencies with their types; a package not written in
    Starlark has none."""
    pkg = node.package
    return pkg.declared_edges() if isinstance(pkg, StarPackage) else []


def _deps(node) -> List:
    return [dep for dep, _ in _edges(node)]


def _is_kaem(node) -> bool:
    pkg = node.package
    return isinstance(pkg, StarPackage) and pkg.kaem_step is not None


def _is_seed(node) -> bool:
    pkg = node.package
    return isinstance(pkg, StarPackage) and pkg.is_seed


def _exec(node) -> Dict[str, Any]:
    """What a dependent may run through ``node`` (Spack's RUNTIME_EXECUTABLE): its run
    dependencies, and theirs and its link dependencies', by DAG hash."""
    out: Dict[str, Any] = {}
    for dep, deptypes in _edges(node):
        if "run" in deptypes:
            out[dep.dag_hash()] = dep
        if "run" in deptypes or "link" in deptypes:
            out.update(_exec(dep))
    return out


def _runnable(node) -> Dict[str, Any]:
    """Whose ``bin`` a build of ``node`` has on PATH, as in Spack's effective_deptypes:
    its build (and test) dependencies, and what each of those runs."""
    out: Dict[str, Any] = {}
    for dep, deptypes in _edges(node):
        if "build" in deptypes or "test" in deptypes:
            out[dep.dag_hash()] = dep
            out.update(_exec(dep))
    return out


def _closure_order(node) -> List:
    """shpack's PATH order for ``node``: a DFS post-order over declared dependencies
    (dependencies before dependents), which compose_path reverses."""
    order: List = []
    seen: Set[str] = set()

    def visit(n) -> None:
        for dep in _deps(n):
            if dep.dag_hash() not in seen:
                seen.add(dep.dag_hash())
                visit(dep)
                order.append(dep)

    visit(node)
    return order


def _node_id(node) -> str:
    return f"{node.name}-{node.version}"


def _seed_path(seed: str, seed_path_file: str) -> List[str]:
    """shpack/bootstrap/seed.path: what the seed puts on the PATH of the steps after it."""
    with open(seed_path_file, encoding="utf-8") as f:
        for line in f:
            if line.startswith("PATH="):
                return line.strip()[len("PATH=") :].replace("${SEED}", seed).split(":")
    raise star_recipe.StarError("seed.path has no PATH= line")


def _kaem_path(step, own_bin: str, seed_path_file: str) -> List[str]:
    """start.kaem's PATH for a kaem step: its own bin, the steps before it (its declared
    dependencies but the seed) newest first, then the seed's (seed.path)."""
    deps = _deps(step)
    seeds = [d for d in deps if _is_seed(d)]
    if len(seeds) != 1:
        raise star_recipe.StarError(f"{step.name}: a kaem step depends on the seed")
    path = [own_bin] + [
        os.path.join(str(d.prefix), "bin") for d in reversed(deps) if not _is_seed(d)
    ]
    return path + _seed_path(str(seeds[0].prefix), seed_path_file)


# ------------------------------------------------------------------ build tools


def _tool(name: str, env: Dict[str, str]) -> str:
    exe = shutil.which(name, path=env["PATH"])
    if not exe:
        raise star_recipe.StarError(f"{name}: not found on the build PATH")
    return exe


def _pipe(first: List[str], second: List[str], cwd: str, env: Dict[str, str], stdin=None):
    """``first | second``, no shell."""
    a = subprocess.Popen(first, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE)
    assert a.stdout is not None
    try:
        subprocess.run(second, cwd=cwd, env=env, stdin=a.stdout, check=True)
    finally:
        a.stdout.close()
        if a.wait() != 0:
            raise subprocess.CalledProcessError(a.returncode, first)


def _unpack(archive: str, into: str, env: Dict[str, str]) -> None:
    """shpack's unpack(): compressors piped into tar, all taken from the build's PATH."""
    tar = [_tool("tar", env), "-xf", "-"]
    if archive.endswith((".tar.gz", ".tgz")):
        _pipe([_tool("gzip", env), "-dc", archive], tar, into, env)
    elif archive.endswith(".tar.bz2"):
        _pipe([_tool("bzip2", env), "-dc", archive], tar, into, env)
    elif archive.endswith((".tar.xz", ".tar.lzma")):
        with open(archive, "rb") as f:
            _pipe([_tool("unxz", env)], tar, into, env, stdin=f)
    elif archive.endswith(".tar"):
        subprocess.run([_tool("tar", env), "-xf", archive], cwd=into, env=env, check=True)
    else:
        subprocess.run([_tool("cp", env), archive, "."], cwd=into, env=env, check=True)


_ARCH_DIR = {"aarch64": "AArch64", "amd64": "AMD64"}


def _copy_input(src: str, dst: str) -> None:
    if os.path.isdir(src):
        copy_tree(src, dst, symlinks=True)
    else:
        mkdirp(os.path.dirname(dst))
        shutil.copy2(src, dst)


def _normalize_modes(prefix: str) -> None:
    """shpack's finalize (chmod -R u=rwX,go=rX): directories 755, files 644, or 755 if any
    execute bit is set."""
    for root, dirs, files in os.walk(prefix):
        os.chmod(root, 0o755)
        for n in files:
            p = os.path.join(root, n)
            if not os.path.islink(p):
                os.chmod(p, 0o755 if os.stat(p).st_mode & 0o111 else 0o644)


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
            raise star_recipe.StarError(f"no file matches {p}")
        return matches

    def one(self, p: str) -> str:
        m = self.expand(p)
        if len(m) != 1:
            raise star_recipe.StarError(f"{p} matches {len(m)} paths, want exactly one")
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
            argv[0] = _tool(argv[0], env)
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
        dst = self.one(a["dst"])
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
                raise star_recipe.StarError(f"copy: {src} is a directory (recursive = True?)")
            copy_tree(src, dst, symlinks=bool(a.get("preserve")))
            return
        if a.get("force") and os.path.lexists(dst) and not os.access(dst, os.W_OK):
            os.remove(dst)
        (shutil.copy2 if a.get("preserve") else shutil.copy)(src, dst)

    def _move(self, a):
        dst = self.one(a["dst"])
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
                        raise star_recipe.StarError(
                            f"remove: {p} is a directory (recursive = True?)"
                        )
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
