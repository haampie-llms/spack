# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Package recipes written in Starlark (``package.star``).

A ``package.star`` is a recipe in the format of shpack's recipes (see
``star/PROTOCOL.md`` in shpack): directives declare versions, dependencies and
patches, and phase functions return *actions* instead of performing them. The
file is written in the common subset of Starlark and Python, so shpack's ``star``
and Python (:mod:`spack.starlark_eval`) evaluate it to the same directive record,
from which this module builds an ordinary :class:`spack.package_base.PackageBase`
subclass (so concretization sees versions, dependencies and patches as usual).

Installing is shpack's: its builder, a package of every DAG (shpack-builder), reads the
concrete spec as shpack's state files and does the rest -- the build's environment,
staging, the plan and running it -- as it does for shpack, with ``star`` from the DAG.

A repository loads a ``package.star`` as a package module (``spack.repo._StarLoader``),
which calls :func:`define_package`: the package class derives from :class:`StarPackage`
and holds the recipe's record. The recipe as data (its record, its package text, the
input of the package hash) is :mod:`spack.star_recipe`.

Spack fetches every archive, versions and ``resource()`` alike, unexpanded; staging
copies into the stage all the build reads, before a build sandbox applies (star/PROTOCOL.md
in shpack, Hosts).
"""

import os
import shutil
import subprocess
import types
from typing import Any, Dict, List, Optional, Tuple

import spack.builder
import spack.config
import spack.directives
import spack.directives_meta
import spack.operating_systems
import spack.platforms
import spack.repo
import spack.stage
import spack.star_recipe as star_recipe
import spack.util.naming as nm
from spack.util.filesystem import copy_tree, mkdirp


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
    derives one per recipe). Staging copies in what the build reads; installing runs
    shpack's builder, or, for a version the kaem phase builds, that kaem step."""

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
        if self.has_code:
            version = next(
                d for d in self.star_directives("version") if d["version"] == str(spec.version)
            )
            fname = version["fname"] or os.path.basename(version["url"])
            shutil.copyfile(self.stage.archive_file, os.path.join(distfiles, fname))
        archives = {
            s.resource.name: s.archive_file
            for s in self.stage
            if isinstance(s, spack.stage.ResourceStage)
        }
        for d in self.star_directives("resource"):
            if self._applies(d):
                shutil.copyfile(archives[d["fname"]], os.path.join(distfiles, d["fname"]))
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
            # as shpack registers a kaem-phase prefix
            _normalize_modes(str(prefix))
        else:
            self._builder_install(str(prefix))

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

    def _builder_install(self, prefix: str) -> None:
        """Build as shpack does: by shpack's builder, run from the store (shpack-builder,
        which every DAG reaches through dash-boot). It reads the node's state directory and
        its closure's (shpack/lib/builder.sh), which are the concrete spec written out, and
        derives the rest -- the build's environment, the build shell, the plan -- itself."""
        spec = self.spec
        var = os.path.join(self._inputs, "var")
        closure = sorted((_node_id(n), n) for n in _closure(spec).values())
        for node in [spec] + [n for _, n in closure]:
            files = {
                "name": node.name,
                "prefix": str(node.prefix),
                "kind": "external" if node.external else "kaem" if _is_kaem(node) else "built",
                "edges": "".join(f"{_node_id(d)} {','.join(t)}\n" for d, t in _edges(node)),
            }
            if _is_kaem(node):
                files["step"] = node.package.kaem_step[0]
            _write_files(os.path.join(var, "spec", _node_id(node)), files)
        version = next(
            d for d in self.star_directives("version") if d["version"] == str(spec.version)
        )
        sources = [(version, version["fname"] or "-")] if version["sha256"] else []
        sources += [(d, d["fname"]) for d in self.star_directives("resource") if self._applies(d)]
        files = {
            "version": str(spec.version),
            "deps": "".join(f"{_node_id(d)}\n" for d in _deps(spec)),
            "closure": "".join(f"{id}\n" for id, _ in closure),
            "sources": "".join(f"{d['sha256']} {f} {d['url'] or '-'}\n" for d, f in sources),
            "patches": "".join(
                f"{d['file']} {int(d['level'])}\n"
                for d in self.star_directives("patch")
                if self._applies(d)
            ),
        }
        if not self.parallel:
            files["parallel"] = "false\n"
        _write_files(os.path.join(var, "spec", _node_id(spec)), files)
        jobs = spack.config.determine_number_of_jobs(parallel=True)
        env = {
            "SHPACK_VAR": var,
            "SHPACK_REPO": os.path.dirname(self._in_tree(self.star_dir)),
            "SHPACK_STAR_ROOT": self._in_tree(self.star_root),
            "DISTFILES": os.path.join(self._inputs, "distfiles"),
            "ARCH": star_recipe.arch(spec),
            "JOBS": str(jobs),
            "MAKEFLAGS": f"-j{jobs}",
            "MFLAGS": f"-j{jobs}",
            "TMPDIR": var,
            # Spack keeps or removes its stage
            "SHPACK_KEEP_STAGE": "1",
        }
        builder = os.path.join(_prefix_of(spec, "shpack-builder"), "bin", "shpack-build")
        sh = os.path.join(_prefix_of(spec, "dash-boot"), "bin", "sh")
        subprocess.run([sh, builder, _node_id(spec)], env=env, check=True)


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


def _closure(node) -> Dict[str, Any]:
    """Every node ``node`` depends on, through its declared dependencies, by DAG hash."""
    out: Dict[str, Any] = {}
    for dep in _deps(node):
        if dep.dag_hash() not in out:
            out[dep.dag_hash()] = dep
            out.update(_closure(dep))
    return out


def _prefix_of(node, name: str) -> str:
    """The prefix of the package ``name`` in the closure of ``node``."""
    for dep in _closure(node).values():
        if dep.name == name:
            return str(dep.prefix)
    raise star_recipe.StarError(f"{node.name}: no {name} in its DAG (depends_on dash-boot)")


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


_ARCH_DIR = {"aarch64": "AArch64", "amd64": "AMD64"}


def _write_files(directory: str, files: Dict[str, str]) -> None:
    mkdirp(directory)
    for name, content in files.items():
        if not content.endswith("\n") and content:
            content += "\n"
        with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
            f.write(content)


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
