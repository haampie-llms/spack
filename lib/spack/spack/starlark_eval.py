# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Evaluate Starlark package recipes (``package.star``) with the Python interpreter.

The recipe dialect (shpack's ``star/DIALECT.md``) is, syntactically, a subset of Python,
and for the constructs recipes use it means the same thing. So a recipe can be evaluated
by executing it as Python in a namespace that holds only what the recipe protocol
(``star/PROTOCOL.md``) predeclares: the directives, the action constructors, ``load``,
``struct``, ``fail`` and the Starlark builtins, with no ``import`` and no I/O.

This evaluator does not *check* the dialect: Python accepts programs Starlark rejects
(recursion, ``while``, floats, reassigned globals). The ``star`` interpreter remains the
checker for recipe authors. For recipes that are valid Starlark, this evaluator and
``star`` produce the same records -- which is what ``star/tests/corpus.sh`` verifies.

The two entry points mirror ``star recipe`` and ``star plan`` and return the same JSON
records (as Python objects).
"""

import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple


class StarlarkError(Exception):
    """A recipe failed to evaluate (``fail()``, a bad directive, a bad action)."""


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


def parse_when(when: str) -> Tuple[Optional[str], Optional[str]]:
    """``@=VERSION`` and ``target=FAMILY:`` terms -> (version, arch)."""
    version = arch = None
    for term in when.split():
        if term.startswith("@=") and len(term) > 2 and version is None:
            version = term[2:]
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


def _strings(x, what: str, depth: int = 0) -> List[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, (list, tuple)) and depth < 2:
        return [s for e in x for s in _strings(e, what, depth + 1)]
    raise StarlarkError(f"{what}: got {_type(x)}, want string or list of strings")


def _paths(x, what: str) -> List[str]:
    paths = _strings(x, what)
    if not paths:
        raise StarlarkError(f"{what}: no paths given")
    return paths


def _action(op: str, **fields) -> Dict[str, Any]:
    """An action as the protocol's JSON: op plus the set fields, sorted by name.
    Absent optional fields and false flags are omitted, as star does."""
    fields = {k: v for k, v in fields.items() if v is not None and v is not False}
    fields["op"] = op
    return dict(sorted(fields.items()))


def _flag(x) -> Optional[bool]:
    return True if x else None


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODE = re.compile(r"^[0-7]{3,4}$")


def _env_name(name: str, what: str) -> str:
    if not _ENV_NAME.match(name):
        raise StarlarkError(f'{what}: invalid environment variable name "{name}"')
    return name


def _mode(mode: str, what: str) -> str:
    if not _MODE.match(mode):
        raise StarlarkError(f'{what}: mode must be octal digits like "755", got "{mode}"')
    return mode


def _run(*argv, cwd=None, env=None, stdout=None):
    args = [s for a in argv for s in _strings(a, "run")]
    if not args:
        raise StarlarkError("run: empty command")
    if env is not None:
        for k in env:
            _env_name(k, "run")
    return _action("run", argv=args, cwd=cwd, env=env, stdout=stdout)


def _chmod(mode, paths):
    return _action("chmod", mode=_mode(mode, "chmod"), paths=_paths(paths, "chmod"))


def _write_file(path, content, mode=None):
    return _action(
        "write_file", path=path, content=content, mode=_mode(mode, "write_file") if mode else None
    )


def _symlink_each(targets, dir, prefix="", if_missing=False, relative=False, exclude=None):
    return _action(
        "symlink_each",
        targets=_paths(targets, "symlink_each"),
        dir=dir,
        prefix=prefix or None,
        if_missing=_flag(if_missing),
        relative=_flag(relative),
        exclude=exclude,
    )


_ACTIONS: Dict[str, Callable] = {
    "run": _run,
    "sh": lambda script, cwd=None: _action("sh", script=script, cwd=cwd),
    "setenv": lambda name, value: _action("setenv", name=_env_name(name, "setenv"), value=value),
    "prepend_path": lambda name, value: _action(
        "prepend_path", name=_env_name(name, "prepend_path"), value=value
    ),
    "unsetenv": lambda name: _action("unsetenv", name=_env_name(name, "unsetenv")),
    "chdir": lambda path: _action("chdir", path=path),
    "mkdir": lambda *paths: _action("mkdir", paths=_paths(list(paths), "mkdir")),
    "copy": lambda src, dst, recursive=False, preserve=False, force=False: _action(
        "copy",
        src=_paths(src, "copy"),
        dst=dst,
        recursive=_flag(recursive),
        preserve=_flag(preserve),
        force=_flag(force),
    ),
    "move": lambda src, dst: _action("move", src=_paths(src, "move"), dst=dst),
    "remove": lambda paths, recursive=False: _action(
        "remove", paths=_paths(paths, "remove"), recursive=_flag(recursive)
    ),
    "symlink": lambda target, link, force=False, if_missing=False: _action(
        "symlink", target=target, link=link, force=_flag(force), if_missing=_flag(if_missing)
    ),
    "hardlink": lambda target, link, force=False, if_missing=False: _action(
        "hardlink", target=target, link=link, force=_flag(force), if_missing=_flag(if_missing)
    ),
    "symlink_each": _symlink_each,
    "chmod": _chmod,
    "write_file": _write_file,
    "append_file": lambda path, content: _action("append_file", path=path, content=content),
    "substitute": lambda files, old, new: _action(
        "substitute", files=_paths(files, "substitute"), old=old, new=new
    ),
    "filter_file": lambda files, regex, repl: _action(
        "filter_file", files=_paths(files, "filter_file"), regex=regex, repl=repl
    ),
}


# -------------------------------------------------------------------- evaluation


class _Evaluation:
    """One recipe evaluation: its module cache, directive record and load order."""

    def __init__(self, packages_path: str, root: str):
        self.packages_path = packages_path
        self.root = root
        self.modules: Dict[str, Dict[str, Any]] = {}
        self.load_bound: Dict[str, set] = {}
        self.load_order: List[str] = []
        self.loading = False
        self.package: Dict[str, Optional[str]] = {}
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
        globs.update(_ACTIONS)
        globs.update(self._directives())
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
        hidden = set(_ACTIONS) | set(self._directives()) | {"load", "__file__", "__builtins__"}
        return {
            k: v
            for k, v in globs.items()
            if not k.startswith("_") and k not in hidden and k not in self.load_bound[path]
        }

    # -- directives --

    def _directives(self) -> Dict[str, Callable]:
        return {
            "package": self._package,
            "version": self._version,
            "resource": self._resource,
            "depends_on": self._depends_on,
            "patch": self._patch,
            "build_system": self._build_system,
            "parallel": self._parallel,
            "build_directory": self._build_directory,
        }

    def _directive(self, kind: str, **fields) -> None:
        if not self.loading:
            raise StarlarkError(f"{kind}: directives may only be called while the recipe loads")
        when = fields.get("when")
        if when is not None:
            _, arch = parse_when(when)
            if arch and kind != "patch":
                raise StarlarkError(
                    f'{kind}: when="{when}": target= constraints are only supported on patch()'
                )
        self.directives.append(dict(directive=kind, **fields))

    def _package(self, description=None, homepage=None, license=None):
        if not self.loading:
            raise StarlarkError("package: directives may only be called while the recipe loads")
        if self.package:
            raise StarlarkError("package: called more than once")
        self.package = {"description": description, "homepage": homepage, "license": license}

    @staticmethod
    def _fname(url, fname):
        if fname:
            return fname
        return url.rsplit("/", 1)[-1] if url else None

    def _version(self, version, sha256=None, url=None, fname=None):
        self._directive(
            "version", version=version, sha256=sha256, url=url, fname=self._fname(url, fname)
        )

    def _resource(self, url=None, sha256=None, fname=None, when=None):
        self._directive(
            "resource", sha256=sha256, url=url, fname=self._fname(url, fname), when=when
        )

    def _depends_on(self, *specs, when=None):
        if not specs:
            raise StarlarkError("depends_on: at least one spec is required")
        for spec in specs:
            self._directive("depends_on", spec=spec, when=when)

    def _patch(self, file, level=1, when=None):
        self._directive("patch", file=file, level=level, when=when)

    def _build_system(self, name, when=None):
        self._directive("build_system", name=name, when=when)

    def _parallel(self, value):
        if not isinstance(value, bool):
            raise StarlarkError(f"parallel: got {_type(value)}, want bool")
        self._directive("parallel", value=value)

    def _build_directory(self, path):
        self._directive("build_directory", path=path)

    # -- the recipe --

    def load_recipe(self, name: str) -> Dict[str, Any]:
        path = os.path.join(self.packages_path, name, "package.star")
        self.loading = True
        try:
            globs = self.module(path)
        finally:
            self.loading = False
        if not self.package:
            raise StarlarkError(f"{path}: package() was not called")
        for d in self.directives:
            if d["directive"] == "build_system":
                self.module(os.path.join(self.root, "build_systems", d["name"] + ".star"))
        return globs

    def loads(self) -> List[str]:
        repo = self.packages_path.rstrip(os.sep) + os.sep
        return [os.path.relpath(p, self.root) for p in self.load_order if not p.startswith(repo)]


def recipe(packages_path: str, root: str, name: str) -> Dict[str, Any]:
    """The directive record of ``name`` (``star recipe --format json``)."""
    ev = _Evaluation(packages_path, root)
    ev.load_recipe(name)
    return {"name": name, "package": ev.package, "directives": ev.directives, "loads": ev.loads()}


def plan(packages_path: str, root: str, name: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The action lists of ``name``'s phases for the build context ``ctx``
    (``star plan --format json``)."""
    ev = _Evaluation(packages_path, root)
    globs = ev.load_recipe(name)
    version, arch = ctx["version"], ctx["arch"]

    def satisfies(when):
        want_version, want_arch = parse_when(when)
        return (want_version in (None, version)) and (want_arch in (None, arch))

    def dep(dep_name):
        if dep_name not in ctx["deps"]:
            raise StarlarkError(
                f"dep: '{dep_name}' is not in the dependency closure of {ctx['id']}"
            )
        return Struct("dep", prefix=ctx["deps"][dep_name])

    bs_name = "generic"
    build_directory = None
    for d in ev.directives:
        if d["directive"] == "build_system" and bs_name == "generic":
            if not d["when"] or parse_when(d["when"])[0] == version:
                bs_name = d["name"]
        if d["directive"] == "build_directory":
            build_directory = d["path"]
    bs = ev.module(os.path.join(root, "build_systems", bs_name + ".star"))

    fields = {k: v for k, v in ctx.items() if k != "deps"}
    fields.update(
        dep=dep,
        satisfies=satisfies,
        build_directory=build_directory,
        pkg=Struct("package", **ev.exported(globs)),
    )
    context = Struct("ctx", **fields)

    def phase_fn(phase):
        exported = ev.exported(globs)
        return exported.get(phase) or ev.exported(bs).get(phase)

    phases = []
    setup = phase_fn("setup_build_environment")
    if setup:
        actions = list(setup(context))
        for a in actions:
            if a["op"] not in ("setenv", "prepend_path", "unsetenv"):
                raise StarlarkError(
                    "setup_build_environment: only setenv/prepend_path/unsetenv actions "
                    f"are allowed, got {a['op']}"
                )
        phases.append({"phase": "setup_build_environment", "actions": actions})
    for phase in ev.exported(bs)["phases"]:
        fn = phase_fn(phase)
        if fn is None:
            raise StarlarkError(f"{name}: build system {bs_name} has no default for {phase}")
        phases.append({"phase": phase, "actions": list(fn(context))})
    return {"id": ctx["id"], "phases": phases}
