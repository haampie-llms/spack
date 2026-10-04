# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Evaluate Starlark package recipes (``package.star``) with the Python interpreter.

The recipe dialect (shpack's ``star/DIALECT.md``) is, syntactically, a subset of Python,
and for the constructs recipes use it means the same thing. So a recipe can be evaluated
by executing it as Python in a namespace that holds only what the recipe protocol
(``star/PROTOCOL.md``) predeclares for loading a recipe: the directives, ``load``,
``struct``, ``fail`` and the Starlark builtins, with no ``import`` and no I/O. Phase
functions are defined but never called here: shpack's builder, which Spack runs to
install (:mod:`spack.star_package`), plans a build with ``star`` itself.

This evaluator does not *check* the dialect: Python accepts programs Starlark rejects
(recursion, ``while``, floats, reassigned globals). The ``star`` interpreter remains the
checker for recipe authors. For recipes that are valid Starlark, this evaluator and
``star`` produce the same records -- which is what ``star/tests/corpus.sh`` verifies.

The entry point, :func:`recipe`, mirrors ``star recipe`` and returns the same JSON record
(as Python objects).
"""

import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple


class StarlarkError(Exception):
    """A recipe failed to evaluate (``fail()``, a bad directive)."""


# ------------------------------------------------------------------------ values


class Struct:
    """Starlark ``struct``: immutable, fields listed in name order."""

    def __init__(self, _ctor: str = "struct", **fields):
        object.__setattr__(self, "_ctor", _ctor)
        object.__setattr__(self, "_fields", dict(sorted(fields.items())))

    def __getattr__(self, name):
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(f"{self._ctor} has no .{name} field or method") from None

    def __setattr__(self, name, value):
        raise StarlarkError(f"can't assign to .{name} field of {self._ctor}")

    def __eq__(self, other):
        return (
            isinstance(other, Struct)
            and self._ctor == other._ctor
            and self._fields == other._fields
        )

    def __hash__(self):
        return hash((self._ctor, tuple(self._fields.items())))

    def __dir__(self):
        return list(self._fields)


def _type(x) -> str:
    if isinstance(x, Struct):
        return x._ctor
    return {
        type(None): "NoneType",
        bool: "bool",
        int: "int",
        str: "string",
        list: "list",
        tuple: "tuple",
        dict: "dict",
        range: "range",
    }.get(type(x), "function" if callable(x) else type(x).__name__)


def _fail(*args, sep=" "):
    raise StarlarkError("fail: " + sep.join(str(a) for a in args))


def _struct(**kwargs):
    return Struct("struct", **kwargs)


# Builtins with Starlark's results where Python's differ (lists, not iterators; type
# names as strings). No import, no open, no eval.
_UNIVERSE: Dict[str, Any] = {
    "None": None,
    "True": True,
    "False": False,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "dir": lambda x: sorted(dir(x)),
    "enumerate": lambda x, start=0: list(enumerate(x, start)),
    "fail": _fail,
    "getattr": getattr,
    "hasattr": hasattr,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "print": lambda *args, sep=" ": print(*args, sep=sep, file=sys.stderr),
    "range": range,
    "repr": repr,
    "reversed": lambda x: list(reversed(list(x))),
    "sorted": sorted,
    "str": str,
    "struct": _struct,
    "tuple": tuple,
    "type": _type,
    "zip": lambda *xs: list(zip(*xs)),
}

# ------------------------------------------------------------------- when= specs

_ARCHES = {"target=x86_64:": "amd64", "target=aarch64:": "aarch64"}


def parse_when(when: str) -> Tuple[Optional[Tuple[str, ...]], Optional[str]]:
    """``@=VERSION`` (or ``@=V1,=V2``) and ``target=FAMILY:`` terms -> (versions, arch)."""
    version: Optional[Tuple[str, ...]] = None
    arch = None
    for term in when.split(" "):
        if not term:
            continue
        if term.startswith("@=") and len(term) > 2:
            if version is not None:
                raise StarlarkError(f'when="{when}": more than one @= constraint')
            items = term[1:].split(",")
            if any(len(i) < 2 or not i.startswith("=") for i in items):
                raise StarlarkError(
                    f'when="{when}": use @=V1,=V2 for a list of exact versions '
                    "(version ranges are not supported)"
                )
            version = tuple(i[1:] for i in items)
        elif term in _ARCHES:
            arch = _ARCHES[term]
        elif term.startswith("@"):
            raise StarlarkError(
                f'when="{when}": use @=VERSION for an exact version '
                "(version ranges are not supported)"
            )
        else:
            raise StarlarkError(
                f'when="{when}": only @=VERSION and target=x86_64: / target=aarch64: are supported'
            )
    if version is None and arch is None:
        raise StarlarkError(f'when="{when}": empty constraint')
    return version, arch


# ---------------------------------------------------------------------- actions


# -------------------------------------------------------------------- evaluation


_DEP_TYPES = ("build", "link", "run", "test")


def dep_types(type) -> List[str]:
    """Spack's ``type=``: a string or a tuple of them, ``("build", "link")`` by default;
    returned in canonical order."""
    if type is None:
        return ["build", "link"]
    if isinstance(type, str):
        type = [type]
    if not isinstance(type, (list, tuple)):
        raise StarlarkError(
            f"depends_on: type= wants a string or a tuple of strings, got {_type(type)}"
        )
    if not type:
        raise StarlarkError("depends_on: type= is empty")
    for t in type:
        if t not in _DEP_TYPES:
            raise StarlarkError(f'depends_on: type "{t}": want build, link, run or test')
    return [t for t in _DEP_TYPES if t in type]


def _conditional(*values, when=None):
    """Spack's ``conditional("makefile", when="@=3.0.4")``, for ``build_system``."""
    if not values:
        raise StarlarkError("conditional: at least one value is required")
    if when is None:
        raise StarlarkError("conditional: when= is required")
    return Struct("conditional", values=tuple(values), when=when)


def _attributes(globs: Dict[str, Any], path: str) -> Dict[str, Any]:
    """What a Spack package keeps in class attributes, a recipe keeps in its docstring and
    globals; in the order of star's record."""
    doc = globs.get("__doc__")
    homepage = globs.get("homepage")
    parallel = globs.get("parallel", True)
    build_directory = globs.get("build_directory")
    if homepage is not None and not isinstance(homepage, str):
        raise StarlarkError(f"{path}: homepage must be a string, got {_type(homepage)}")
    if not isinstance(parallel, bool):
        raise StarlarkError(f"{path}: parallel must be a bool, got {_type(parallel)}")
    if build_directory is not None:
        if not isinstance(build_directory, str):
            raise StarlarkError(
                f"{path}: build_directory must be a string, got {_type(build_directory)}"
            )
        if not build_directory or build_directory.startswith("/"):
            raise StarlarkError(f"{path}: build_directory must be a relative path")
    description = " ".join(w for w in re.split("[ \t\n\r]+", doc) if w) if doc else None
    return {
        "description": description or None,
        "homepage": homepage,
        "parallel": parallel,
        "build_directory": build_directory,
    }


class _Evaluation:
    """One recipe evaluation: its module cache, directive record and load order."""

    def __init__(self, packages_path: str, root: str):
        self.packages_path = packages_path
        self.root = root
        self.modules: Dict[str, Dict[str, Any]] = {}
        self.load_bound: Dict[str, set] = {}
        self.load_order: List[str] = []
        self.attributes: Dict[str, Any] = {}
        self.directives: List[Dict[str, Any]] = []

    # -- modules --

    def _resolve(self, name: str, from_file: str) -> str:
        if name.startswith("//"):
            return os.path.join(self.root, name[2:])
        if name.startswith("/") or ".." in name:
            raise StarlarkError(f'load: "{name}": use //path or a path below the loading file')
        return os.path.join(os.path.dirname(from_file), name)

    def module(self, path: str) -> Dict[str, Any]:
        if path in self.modules:
            return self.modules[path]
        globs: Dict[str, Any] = {"__builtins__": _UNIVERSE, "__file__": path}
        globs.update(self._directives())
        globs["conditional"] = _conditional
        globs["load"] = self._loader(path, globs)
        self.load_bound[path] = set()
        self.modules[path] = globs
        self.load_order.append(path)  # in the order loads start, as star lists them
        with open(path, "r", encoding="utf-8") as f:
            code = compile(f.read(), path, "exec")
        exec(code, globs)  # the recipe is data plus pure functions; see module docstring
        return globs

    def _loader(self, from_file: str, globs: Dict[str, Any]) -> Callable:
        def load(name, *symbols, **aliases):
            mod = self.module(self._resolve(name, from_file))
            wanted = [(s, s) for s in symbols] + list(aliases.items())
            for local, remote in wanted:
                if remote.startswith("_") or remote not in self.exported(mod):
                    raise StarlarkError(f"load: name {remote} not found in module {name}")
                globs[local] = mod[remote]
                self.load_bound[from_file].add(local)

        return load

    def exported(self, globs: Dict[str, Any]) -> Dict[str, Any]:
        path = globs["__file__"]
        hidden = set(self._directives()) | {"load", "conditional"}
        return {
            k: v
            for k, v in globs.items()
            if not k.startswith("_") and k not in hidden and k not in self.load_bound[path]
        }

    # -- directives --

    def _directives(self) -> Dict[str, Callable]:
        return {
            "version": self._version,
            "resource": self._resource,
            "depends_on": self._depends_on,
            "patch": self._patch,
            "license": self._license,
            "build_system": self._build_system,
            "when": self._when,
        }

    def _when(self, cond, items):
        """``when("@=4.9-musl", [depends_on(...), ...])``: Spack's ``with when()``. The
        directives in the list are declared already; their conditions become their own AND
        this one. Returns them, so that when() nests."""
        if not isinstance(cond, str):
            raise StarlarkError(f"when: got {_type(cond)}, want string")
        try:
            versions, arch = parse_when(cond)
        except StarlarkError as e:
            raise StarlarkError(f'when("{cond}"): {str(e).split(": ", 1)[1]}') from None
        if not isinstance(items, (list, tuple)):
            raise StarlarkError(f"when: got {_type(items)}, want a list of directives")
        out: List[Struct] = []
        self._when_apply(items, versions, arch, cond, out, 0)
        return out

    def _when_apply(self, x, versions, arch, cond, out, depth):
        if isinstance(x, (list, tuple)):
            if depth > 8:
                raise StarlarkError("when: lists nested too deeply")
            for item in x:
                self._when_apply(item, versions, arch, cond, out, depth + 1)
            return
        if isinstance(x, Struct) and x._ctor == "directive":
            self._conjoin(self.directives[x.index], versions, arch, cond)
            out.append(x)
            return
        raise StarlarkError(
            f"when: got {_type(x)}, want the value of depends_on, patch, resource or license "
            "(or a list of them)"
        )

    @staticmethod
    def _conjoin(d, versions, arch, cond):
        """``d``'s condition AND (versions, arch), written back in canonical form
        ``@=V1,=V2 target=FAMILY:``; version lists intersect, in ``d``'s order."""
        kind, old = d["directive"], d["when"]
        v, ar = parse_when(old) if old is not None else (None, None)
        if versions and v:
            v = tuple(x for x in v if x in versions)
            if not v:
                raise StarlarkError(
                    f'when("{cond}"): no version satisfies both it and the {kind}\'s when="{old}"'
                )
        elif versions:
            v = versions
        if arch and ar and arch != ar:
            raise StarlarkError(
                f'when("{cond}"): the {kind}\'s when="{old}" asks for another target'
            )
        ar = arch or ar
        if ar and kind != "patch":
            raise StarlarkError(
                f'when("{cond}"): target= constraints are only supported on patch()'
            )
        parts = []
        if v:
            parts.append("@" + ",".join("=" + x for x in v))
        if ar:
            parts.append("target=x86_64:" if ar == "amd64" else "target=aarch64:")
        d["when"] = " ".join(parts)

    def _directive(self, kind: str, **fields) -> "Struct":
        when = fields.get("when")
        if when is not None:
            _, arch = parse_when(when)
            if arch and kind != "patch":
                raise StarlarkError(
                    f'{kind}: when="{when}": target= constraints are only supported on patch()'
                )
        self.directives.append(dict(directive=kind, **fields))
        # the value when() constrains: the directive's index in the record
        return Struct("directive", index=len(self.directives) - 1)

    @staticmethod
    def _fname(url, fname):
        if fname:
            return fname
        return url.rsplit("/", 1)[-1] if url else None

    def _version(self, version, sha256=None, url=None, fname=None):
        self._directive(
            "version", version=version, sha256=sha256, url=url, fname=self._fname(url, fname)
        )

    def _resource(
        self, url=None, sha256=None, fname=None, destination=None, placement=None, when=None
    ):
        destination = destination or None  # Spack's default, ""
        for what, path in (("destination", destination), ("placement", placement)):
            parts = (path or "x").split("/")
            if path is not None and (
                path.startswith("/") or any(p in ("", ".", "..") for p in parts)
            ):
                raise StarlarkError(f'resource: {what} "{path}" must be a relative path')
        if placement and "/" in placement:
            raise StarlarkError(f'resource: placement "{placement}" is a directory name')
        return self._directive(
            "resource",
            sha256=sha256,
            url=url,
            fname=self._fname(url, fname),
            destination=destination,
            placement=placement,
            when=when,
        )

    def _depends_on(self, spec, when=None, type=None):
        if not spec or spec.startswith("@") or any(c in spec for c in " \t\n"):
            raise StarlarkError(f'depends_on: invalid spec "{spec}"')
        _, at, version = spec.partition("@")
        if at and (
            not version.startswith("=") or len(version) < 2 or "@" in version or "," in version
        ):
            raise StarlarkError(
                f'depends_on: "{spec}": use name@=VERSION for an exact version '
                "(version ranges are not supported)"
            )
        return self._directive("depends_on", spec=spec, type=dep_types(type), when=when)

    def _patch(self, file, level=1, when=None):
        return self._directive("patch", file=file, level=level, when=when)

    def _license(self, license_identifier, checked_by=None, when=None):
        if not license_identifier:
            raise StarlarkError("license: empty license identifier")
        return self._directive("license", license=license_identifier, when=when)

    def _build_system(self, *values, default=None):
        if not values:
            raise StarlarkError("build_system: at least one value is required")
        if any(d["directive"] == "build_system" for d in self.directives):
            raise StarlarkError("build_system: called more than once")
        out = []
        for v in values:
            if isinstance(v, Struct) and v._ctor == "conditional":
                out.extend({"name": n, "when": v.when} for n in v.values)
            else:
                out.append({"name": v, "when": None})
        for v in out:
            if v["when"] is not None:
                parse_when(v["when"])
        default = default or out[0]["name"]
        if default not in [v["name"] for v in out]:
            raise StarlarkError(f'build_system: default "{default}" is not among the values')
        self._directive("build_system", values=out, default=default)

    # -- the recipe --

    def load_recipe(self, name: str) -> Dict[str, Any]:
        path = os.path.join(self.packages_path, name, "package.star")
        globs = self.module(path)
        self.attributes = _attributes(globs, path)
        for d in self.directives:
            if d["directive"] == "build_system":
                for v in d["values"]:
                    self.module(os.path.join(self.root, "build_systems", v["name"] + ".star"))
        return globs

    def loads(self) -> List[str]:
        """The loaded modules, relative to the root and with ``/`` as star records them
        (they are in the package text, so in the package hash, on any host)."""
        repo = self.packages_path.rstrip(os.sep) + os.sep
        return [
            os.path.relpath(p, self.root).replace(os.sep, "/")
            for p in self.load_order
            if not p.startswith(repo)
        ]


def recipe(packages_path: str, root: str, name: str) -> Dict[str, Any]:
    """The directive record of ``name`` (``star recipe --format json``)."""
    ev = _Evaluation(packages_path, root)
    ev.load_recipe(name)
    return {"name": name, **ev.attributes, "directives": ev.directives, "loads": ev.loads()}
