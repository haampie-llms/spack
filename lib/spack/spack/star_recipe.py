# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""A Starlark package recipe (``package.star``) as data: its directive record, its
package directory, and its package text, the input of its package hash. Kept apart from
:mod:`spack.star_package`, which makes it a package class, so that the package hash
(:mod:`spack.util.package_hash`) needs only this and the evaluator."""

import hashlib
import os
from typing import Any, Dict, List

import spack.starlark_eval
from spack.error import SpackError

#: shpack's arch names, by target family
ARCH = {"x86_64": "amd64", "aarch64": "aarch64"}

#: The evaluator shpack names in its package text (star --version)
STAR_VERSION = "star 1.0"

#: The operating system of every Starlark package: what they build does not depend on the
#: host's, and shpack records it (platform_os) so that both compute the same hashes
STAR_OS = "shpack"


class StarError(SpackError):
    """Error evaluating or executing a Starlark package recipe."""


#: evaluated records, by package directory
records_cache: Dict[str, Any] = {}


def record(packages_path: str, root: str, name: str) -> Any:
    key = os.path.join(packages_path, name)
    if key not in records_cache:
        try:
            records_cache[key] = spack.starlark_eval.recipe(packages_path, root, name)
        except spack.starlark_eval.StarlarkError as e:
            raise StarError(f"{name}: {e}") from e
    return records_cache[key]


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def package_files(pkg_dir: str, rel: str = "") -> List:
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
            out.extend(package_files(pkg_dir, r))
        else:
            out.append((r, sha256_file(path)))
    return out


def kaem_steps(pkg_dir: str) -> Dict[str, List[str]]:
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
    rec = record(packages_path, root, os.path.basename(pkg_dir))
    version = str(spec.version)
    family = str(spec.target.family)
    out = [f"package {spec.name}", f"version {version}", f"arch {ARCH.get(family, family)}"]
    for d in rec["directives"]:
        if d["directive"] == "version" and d["version"] == version and d["sha256"]:
            out.append(f"source {d['sha256']} {d['fname'] or '-'}")
    for d in rec["directives"]:
        if d["directive"] == "resource" and (not d["when"] or spec.satisfies(d["when"])):
            out.append(f"source {d['sha256']} {d['fname']}")
    out.extend(f"file {sha} {path}" for path, sha in package_files(pkg_dir))
    # a kaem step also depends on the tree it runs, by content
    # ("!PATH" leaves out what is under PATH: the seed's own build outputs)
    tree = os.path.dirname(root)
    inputs = kaem_steps(pkg_dir).get(version, [])[1:]
    skip = tuple(p[1:] + "/" for p in inputs if p.startswith("!"))
    for path in inputs:
        if path.startswith("!"):
            continue
        full = os.path.join(tree, path)
        if not os.path.isdir(full):
            out.append(f"input {sha256_file(full)} {path}")
            continue
        for rel, sha in package_files(full):
            if not f"{path}/{rel}".startswith(skip):
                out.append(f"input {sha} {path}/{rel}")
    out.append(f"evaluator {STAR_VERSION}")
    out.extend(f"load {sha256_file(os.path.join(root, m))} {m}" for m in rec["loads"])
    return "".join(line + "\n" for line in out)
