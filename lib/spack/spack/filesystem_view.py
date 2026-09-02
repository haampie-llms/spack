# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import functools as ft
import io
import os
import re
import shutil
import stat
import sys
import tempfile
from typing import IO, Callable, Dict, List, Optional, Tuple

from spack.vendor.typing_extensions import Literal

import spack.config
import spack.directory_layout
import spack.projections
import spack.relocate
import spack.relocate_text
import spack.schema.projections
import spack.spec
import spack.store
import spack.util.filesystem as fs
import spack.util.spack_json as s_json
import spack.util.spack_yaml as s_yaml
from spack.error import SpackError
from spack.util import tty
from spack.util.filesystem import (
    mkdirp,
    remove_dead_links,
    remove_empty_directories,
    symlink,
    visit_directory_tree,
)
from spack.util.lang import index_by, match_predicate
from spack.util.link_tree import (
    ConflictingSpecsError,
    DestinationMergeVisitor,
    LinkTree,
    MergeConflictSummary,
    MultiPrefixMerger,
    SingleMergeConflictError,
)
from spack.util.string import comma_or
from spack.util.tty.color import colorize

_projections_path = ".spack/projections.yaml"


LinkCallbackType = Callable[[str, str, "FilesystemView", Optional[spack.spec.Spec]], None]


def view_symlink(src: str, dst: str, view: "FilesystemView", *args, **kwargs) -> None:
    dir_fd, target = view.destination_of(dst)
    symlink(src, target, dir_fd=dir_fd)


def view_hardlink(src: str, dst: str, view: "FilesystemView", *args, **kwargs) -> None:
    dir_fd, target = view.destination_of(dst)
    os.link(src, target, dst_dir_fd=dir_fd)


def view_copy(
    src: str, dst: str, view: "FilesystemView", spec: Optional[spack.spec.Spec] = None
) -> None:
    """
    Copy a file from src to dst.

    Use spec and view to generate relocations. Relocation is done in memory and the result is
    written to the view's destination, so the file at ``dst`` is never opened by path.
    """
    dir_fd, target = view.destination_of(dst)
    src_stat = os.lstat(src)

    # No need to relocate if no metadata or external. Order of this dict is somewhat irrelevant.
    prefix_to_projection: Dict[str, str] = (
        {
            str(s.prefix): view.get_projection_for_spec(s)
            for s in spec.traverse(root=True, order="breadth")
            if not s.external
        }
        if spec and not spec.external
        else {}
    )

    if stat.S_ISLNK(src_stat.st_mode):
        link_target = os.readlink(src)
        if prefix_to_projection and os.path.isabs(link_target):
            regex = re.compile("|".join(re.escape(p) for p in prefix_to_projection))
            match = regex.match(link_target)
            if match is not None:
                link_target = prefix_to_projection[match.group()] + link_target[match.end() :]
        symlink(link_target, target, dir_fd=dir_fd)
        if sys.platform != "win32":
            try:
                os.chown(
                    target, src_stat.st_uid, src_stat.st_gid, dir_fd=dir_fd, follow_symlinks=False
                )
            except OSError:
                tty.debug(f"Can't change the permissions for {dst}")
        return

    # TODO: change this into a bulk operation instead of a per-file operation
    with open(src, "rb") as f:
        data = f.read()

    if prefix_to_projection:
        buffer = io.BytesIO(data)
        if spack.relocate.is_elf_magic(data[:8]) or spack.relocate.is_macho_magic(data[:8]):
            replacer: spack.relocate_text.PrefixReplacer = (
                spack.relocate_text.BinaryFilePrefixReplacer.from_strings_or_bytes(
                    prefix_to_projection
                )
            )
        else:
            prefix_to_projection[spack.store.STORE.layout.root] = view._root
            replacer = spack.relocate_text.TextFilePrefixReplacer.from_strings_or_bytes(
                prefix_to_projection
            )
        if replacer.apply_to_file(buffer):
            data = buffer.getvalue()

    mode = stat.S_IMODE(src_stat.st_mode)
    fd = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | fs.NOFOLLOW_FLAGS, mode, dir_fd=dir_fd
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if sys.platform != "win32":
                # Like shutil.copy2: copy mode and times, and try to preserve ownership.
                os.chmod(f.fileno(), mode)
                os.utime(f.fileno(), ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
                try:
                    os.chown(f.fileno(), src_stat.st_uid, src_stat.st_gid)
                except OSError:
                    tty.debug(f"Can't change the permissions for {dst}")
    except BaseException:
        try:
            os.unlink(target, dir_fd=dir_fd)
        except OSError:
            pass
        raise

    if sys.platform == "win32":
        from spack.util.win_acl import copy_file_permissions

        try:
            copy_file_permissions(src, target)
        except OSError:
            tty.debug(f"Can't change the permissions for {dst}")


#: Type alias for link types
LinkType = Literal["hardlink", "hard", "copy", "relocate", "add", "symlink", "soft"]
CanonicalLinkType = Literal["hardlink", "copy", "symlink"]


#: supported string values for `link_type` in an env, mapped to canonical values
_LINK_TYPES: Dict[LinkType, CanonicalLinkType] = {
    "hardlink": "hardlink",
    "hard": "hardlink",
    "copy": "copy",
    "relocate": "copy",
    "add": "symlink",
    "symlink": "symlink",
    "soft": "symlink",
}

_VALID_LINK_TYPES = sorted(set(_LINK_TYPES.values()))


def canonicalize_link_type(link_type: LinkType) -> CanonicalLinkType:
    """Return canonical"""
    canonical = _LINK_TYPES.get(link_type)
    if not canonical:
        raise ValueError(
            f"Invalid link type: '{link_type}. Must be one of {comma_or(_VALID_LINK_TYPES)}'"
        )
    return canonical


def function_for_link_type(link_type: LinkType) -> LinkCallbackType:
    link_type = canonicalize_link_type(link_type)
    if link_type == "hardlink":
        return view_hardlink
    elif link_type == "symlink":
        return view_symlink
    elif link_type == "copy":
        return view_copy

    assert False, "invalid link type"


class FilesystemView:
    """
    Governs a filesystem view that is located at certain root-directory.

    Packages are linked from their install directories into a common file
    hierarchy.

    In distributed filesystems, loading each installed package separately
    can lead to slow-downs due to too many directories being traversed.
    This can be circumvented by loading all needed modules into a common
    directory structure.
    """

    def __init__(
        self,
        root: str,
        layout: spack.directory_layout.DirectoryLayout,
        *,
        projections: Optional[Dict] = None,
        ignore_conflicts: bool = False,
        verbose: bool = False,
        link_type: LinkType = "symlink",
        link_dirs: bool = False,
        destination: Optional[str] = None,
        destination_fd: Optional[int] = None,
    ):
        """
        Initialize a filesystem view under the given ``root`` directory with
        corresponding directory ``layout``.

        Files are linked by method ``link`` (spack.util.filesystem.symlink by default).

        ``root`` is the *logical* location of the view: it is what projections and paths baked
        into file contents refer to. By default files are also written there, but a view can be
        staged elsewhere by passing ``destination`` (a directory path) and, on POSIX,
        ``destination_fd`` (an open file descriptor of that directory). All writes then happen
        relative to that descriptor, so they cannot be redirected by renaming the directory, and
        the finished staging directory can be moved to ``root`` in one atomic ``rename``.
        """
        self._root = root
        self._destination = destination if destination is not None else root
        self._destination_fd = destination_fd
        self.layout = layout
        self.projections = {} if projections is None else projections

        self.ignore_conflicts = ignore_conflicts
        self.verbose = verbose

        # Setup link function to include view
        self.link_type = link_type
        self._link = function_for_link_type(link_type)
        self.link_dirs = link_dirs and link_type == "symlink"

    def link(self, src: str, dst: str, spec: Optional[spack.spec.Spec] = None) -> None:
        """Link (or copy, depending on the link type) ``src`` to the path ``dst`` in the view."""
        self._link(src, dst, self, spec)

    def _relative_to_root(self, dst: str) -> str:
        """Return ``dst`` relative to the view root, or raise if it is not inside the view."""
        if dst == self._root:
            return ""
        prefix = os.path.join(self._root, "")
        if not dst.startswith(prefix):
            dst = os.path.normpath(dst)
            prefix = os.path.normpath(prefix) + os.sep
            if not dst.startswith(prefix):
                raise ValueError(f"{dst} is not inside the view root {self._root}")
        return dst[len(prefix) :]

    def destination_of(self, dst: str) -> Tuple[Optional[int], str]:
        """Translate the path ``dst`` in the view to where it has to be written.

        Returns a ``(dir_fd, path)`` pair to be passed to the ``dir_fd`` argument and the path
        argument of the ``os`` functions: either an open descriptor of the staging directory and
        a path relative to it, or ``None`` and an absolute path."""
        rel = self._relative_to_root(dst)
        if self._destination_fd is not None:
            return self._destination_fd, rel or "."
        return None, os.path.join(self._destination, rel) if rel else self._destination

    def exists(self, dst: str) -> bool:
        """Whether the path ``dst`` exists in the view (without following a final symlink)."""
        dir_fd, path = self.destination_of(dst)
        try:
            os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return False
        return True

    def mkdir(self, dst: str) -> None:
        """Create the directory ``dst`` in the view. Its parent must exist."""
        dir_fd, path = self.destination_of(dst)
        os.mkdir(path, dir_fd=dir_fd)

    def open(
        self,
        dst: str,
        mode: str = "w",
        *,
        permissions: int = 0o644,
        encoding: Optional[str] = None,
    ) -> IO:
        """Open the file ``dst`` in the view for writing, and return a file object.

        Packages that generate files in the view (rather than linking them from their prefix)
        must use this instead of ``open(dst, "w")``: the path ``dst`` is where the file ends up
        once the view is published, not necessarily where it is written now.

        Args:
            dst: path of the file in the view
            mode: ``"w"``, ``"wb"``, ``"x"`` or ``"xb"``
            permissions: mode bits of the file if it is created
            encoding: text encoding, for text modes
        """
        if mode not in ("w", "wb", "x", "xb"):
            raise ValueError(f"unsupported mode {mode!r}, expected one of 'w', 'wb', 'x', 'xb'")
        dir_fd, path = self.destination_of(dst)
        flags = os.O_WRONLY | os.O_CREAT | fs.NOFOLLOW_FLAGS
        flags |= os.O_EXCL if mode.startswith("x") else os.O_TRUNC
        fd = os.open(path, flags, permissions, dir_fd=dir_fd)
        try:
            return os.fdopen(fd, mode, encoding=encoding)
        except BaseException:
            os.close(fd)
            raise

    def add_specs(self, *specs: spack.spec.Spec, **kwargs) -> None:
        """
        Add given specs to view.

        Should accept ``with_dependencies`` as keyword argument (default
        True) to indicate whether or not dependencies should be activated as
        well.

        Should except an ``exclude`` keyword argument containing a list of
        regexps that filter out matching spec names.

        This method should make use of ``activate_standalone``.
        """
        raise NotImplementedError

    def add_standalone(self, spec: spack.spec.Spec) -> bool:
        """
        Add (link) a standalone package into this view.
        """
        raise NotImplementedError

    def check_added(self, spec: spack.spec.Spec) -> bool:
        """
        Check if the given concrete spec is active in this view.
        """
        raise NotImplementedError

    def remove_specs(self, *specs: spack.spec.Spec, **kwargs) -> None:
        """
        Removes given specs from view.

        Should accept ``with_dependencies`` as keyword argument (default
        True) to indicate whether or not dependencies should be deactivated
        as well.

        Should accept ``with_dependents`` as keyword argument (default True)
        to indicate whether or not dependents on the deactivated specs
        should be removed as well.

        Should except an ``exclude`` keyword argument containing a list of
        regexps that filter out matching spec names.

        This method should make use of ``deactivate_standalone``.
        """
        raise NotImplementedError

    def remove_standalone(self, spec: spack.spec.Spec) -> None:
        """
        Remove (unlink) a standalone package from this view.
        """
        raise NotImplementedError

    def get_projection_for_spec(self, spec: spack.spec.Spec) -> str:
        """
        Get the projection in this view for a spec.
        """
        raise NotImplementedError

    def get_all_specs(self) -> List[spack.spec.Spec]:
        """
        Get all specs currently active in this view.
        """
        raise NotImplementedError

    def get_spec(self, spec: spack.spec.Spec) -> Optional[spack.spec.Spec]:
        """
        Return the actual spec linked in this view (i.e. do not look it up
        in the database by name).

        ``spec`` can be a name or a spec from which the name is extracted.

        As there can only be a single version active for any spec the name
        is enough to identify the spec in the view.

        If no spec is present, returns None.
        """
        raise NotImplementedError

    def print_status(self, *specs: spack.spec.Spec, **kwargs) -> None:
        """
        Print a short summary about the given specs, detailing whether..

        * ..they are active in the view.
        * ..they are active but the activated version differs.
        * ..they are not active in the view.

        Takes ``with_dependencies`` keyword argument so that the status of
        dependencies is printed as well.
        """
        raise NotImplementedError


class YamlFilesystemView(FilesystemView):
    """
    Filesystem view to work with a yaml based directory layout.
    """

    def __init__(
        self,
        root: str,
        layout: spack.directory_layout.DirectoryLayout,
        *,
        projections: Optional[Dict] = None,
        ignore_conflicts: bool = False,
        verbose: bool = False,
        link_type: LinkType = "symlink",
    ):
        super().__init__(
            root,
            layout,
            projections=projections,
            ignore_conflicts=ignore_conflicts,
            verbose=verbose,
            link_type=link_type,
        )

        # Super class gets projections from the kwargs
        # YAML specific to get projections from YAML file
        self.projections_path = os.path.join(self._root, _projections_path)
        if not self.projections:
            # Read projections file from view
            self.projections = self.read_projections()
        elif not os.path.exists(self.projections_path):
            # Write projections file to new view
            self.write_projections()
        else:
            # Ensure projections are the same from each source
            # Read projections file from view
            if self.projections != self.read_projections():
                raise ConflictingProjectionsError(
                    f"View at {self._root} has projections file"
                    " which does not match projections passed manually."
                )

        self._croot = colorize_root(self._root) + " "

    def write_projections(self):
        if self.projections:
            mkdirp(os.path.dirname(self.projections_path))
            with open(self.projections_path, "w", encoding="utf-8") as f:
                f.write(s_yaml.dump_config({"projections": self.projections}))

    def read_projections(self):
        if os.path.exists(self.projections_path):
            with open(self.projections_path, "r", encoding="utf-8") as f:
                projections_data = s_yaml.load(f)
                spack.config.validate(projections_data, spack.schema.projections.schema)
                return projections_data["projections"]
        else:
            return {}

    def add_specs(self, *specs, **kwargs):
        assert all((s.concrete for s in specs))
        specs = set(specs)

        if kwargs.get("with_dependencies", True):
            specs.update(get_dependencies(specs))

        if kwargs.get("exclude", None):
            specs = set(filter_exclude(specs, kwargs["exclude"]))

        conflicts = self.get_conflicts(*specs)

        if conflicts:
            for s, v in conflicts:
                self.print_conflict(v, s)
            return

        for s in specs:
            self.add_standalone(s)

    def add_standalone(self, spec):
        if spec.external:
            tty.warn(f"{self._croot}Skipping external package: {colorize_spec(spec)}")
            return True

        if self.check_added(spec):
            tty.warn(f"{self._croot}Skipping already linked package: {colorize_spec(spec)}")
            return True

        self.merge(spec)

        self.link_meta_folder(spec)

        if self.verbose:
            tty.info(f"{self._croot}Linked package: {colorize_spec(spec)}")
        return True

    def merge(self, spec, ignore=None):
        pkg = spec.package
        view_source = pkg.view_source()
        view_dst = pkg.view_destination(self)

        tree = LinkTree(view_source)

        ignore = ignore or (lambda f: False)
        ignore_file = match_predicate(self.layout.hidden_file_regexes, ignore)

        # check for dir conflicts
        conflicts = tree.find_dir_conflicts(view_dst, ignore_file)

        merge_map = tree.get_file_map(view_dst, ignore_file)
        if not self.ignore_conflicts:
            conflicts.extend(pkg.view_file_conflicts(self, merge_map))

        if conflicts:
            raise SingleMergeConflictError(conflicts[0])

        # merge directories with the tree
        tree.merge_directories(view_dst, ignore_file)

        pkg.add_files_to_view(self, merge_map)

    def unmerge(self, spec, ignore=None):
        pkg = spec.package
        view_source = pkg.view_source()
        view_dst = pkg.view_destination(self)

        tree = LinkTree(view_source)

        ignore = ignore or (lambda f: False)
        ignore_file = match_predicate(self.layout.hidden_file_regexes, ignore)

        merge_map = tree.get_file_map(view_dst, ignore_file)
        pkg.remove_files_from_view(self, merge_map)

        # now unmerge the directory tree
        tree.unmerge_directories(view_dst, ignore_file)

    def remove_files(self, files):
        def needs_file(spec, file):
            # convert the file we want to remove to a source in this spec
            projection = self.get_projection_for_spec(spec)
            relative_path = os.path.relpath(file, projection)
            test_path = os.path.join(spec.prefix, relative_path)

            # check if this spec owns a file of that name (through the
            # manifest in the metadata dir, which we have in the view).
            manifest_file = os.path.join(
                self.get_path_meta_folder(spec), spack.store.STORE.layout.manifest_file_name
            )
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = s_json.load(f)
            except OSError:
                # if we can't load it, assume it doesn't know about the file.
                manifest = {}
            return test_path in manifest

        specs = self.get_all_specs()

        for file in files:
            if not os.path.lexists(file):
                tty.warn(f"Tried to remove {file} which does not exist")
                continue

            # remove if file is not owned by any other package in the view
            # This will only be false if two packages are merged into a prefix
            # and have a conflicting file

            # check all specs for whether they own the file. That include the spec
            # we are currently removing, as we remove files before unlinking the
            # metadata directory.
            if len([s for s in specs if needs_file(s, file)]) <= 1:
                tty.debug(f"Removing file {file}")
                os.remove(file)

    def check_added(self, spec):
        assert spec.concrete
        return spec == self.get_spec(spec)

    def remove_specs(self, *specs, **kwargs):
        assert all((s.concrete for s in specs))
        with_dependents = kwargs.get("with_dependents", True)
        with_dependencies = kwargs.get("with_dependencies", False)

        # caller can pass this in, as get_all_specs() is expensive
        all_specs = kwargs.get("all_specs", None) or set(self.get_all_specs())

        specs = set(specs)

        if with_dependencies:
            specs = get_dependencies(specs)

        if kwargs.get("exclude", None):
            specs = set(filter_exclude(specs, kwargs["exclude"]))

        to_deactivate = specs
        to_keep = all_specs - to_deactivate

        dependents = find_dependents(to_keep, to_deactivate)

        if with_dependents:
            # remove all packages depending on the ones to remove
            if len(dependents) > 0:
                tty.warn(
                    self._croot
                    + "The following dependents will be removed: %s"
                    % ", ".join((s.name for s in dependents))
                )
                to_deactivate.update(dependents)
        elif len(dependents) > 0:
            tty.warn(
                self._croot
                + "The following packages will be unusable: %s"
                % ", ".join((s.name for s in dependents))
            )

        # Determine the order that packages should be removed from the view;
        # dependents come before their dependencies.
        to_deactivate_sorted = list()
        depmap = dict()
        for spec in to_deactivate:
            depmap[spec] = {d for d in spec.traverse(root=False) if d in to_deactivate}

        while depmap:
            for spec in [s for s, d in depmap.items() if not d]:
                to_deactivate_sorted.append(spec)
                for s in depmap.keys():
                    depmap[s].discard(spec)
                depmap.pop(spec)
        to_deactivate_sorted.reverse()

        # Ensure that the sorted list contains all the packages
        assert set(to_deactivate_sorted) == to_deactivate

        # Remove the packages from the view
        for spec in to_deactivate_sorted:
            self.remove_standalone(spec)

        self._purge_empty_directories()

    def remove_standalone(self, spec):
        """
        Remove (unlink) a standalone package from this view.
        """
        if not self.check_added(spec):
            tty.warn(f"{self._croot}Skipping package not linked in view: {spec.name}")
            return

        self.unmerge(spec)
        self.unlink_meta_folder(spec)

        if self.verbose:
            tty.info(f"{self._croot}Removed package: {colorize_spec(spec)}")

    def get_projection_for_spec(self, spec):
        """
        Return the projection for a spec in this view.

        Relies on the ordering of projections to avoid ambiguity.
        """
        spec = spack.spec.Spec(spec)
        locator_spec = spec

        if spec.package.extendee_spec:
            locator_spec = spec.package.extendee_spec

        proj = spack.projections.get_projection(self.projections, locator_spec)
        if proj:
            return os.path.join(self._root, locator_spec.format_path(proj))
        return self._root

    def get_all_specs(self):
        md_dirs = []
        for root, dirs, files in os.walk(self._root):
            if spack.store.STORE.layout.metadata_dir in dirs:
                md_dirs.append(os.path.join(root, spack.store.STORE.layout.metadata_dir))

        specs = []
        for md_dir in md_dirs:
            if os.path.exists(md_dir):
                for name_dir in os.listdir(md_dir):
                    filename = os.path.join(
                        md_dir, name_dir, spack.store.STORE.layout.spec_file_name
                    )
                    spec = get_spec_from_file(filename)
                    if spec:
                        specs.append(spec)
        return specs

    def get_conflicts(self, *specs):
        """
        Return list of tuples (<spec>, <spec in view>) where the spec
        active in the view differs from the one to be activated.
        """
        in_view = map(self.get_spec, specs)
        return [(s, v) for s, v in zip(specs, in_view) if v is not None and s != v]

    def get_path_meta_folder(self, spec):
        "Get path to meta folder for either spec or spec name."
        return os.path.join(
            self.get_projection_for_spec(spec),
            spack.store.STORE.layout.metadata_dir,
            getattr(spec, "name", spec),
        )

    def get_spec(self, spec):
        dotspack = self.get_path_meta_folder(spec)
        filename = os.path.join(dotspack, spack.store.STORE.layout.spec_file_name)

        return get_spec_from_file(filename)

    def link_meta_folder(self, spec):
        src = spack.store.STORE.layout.metadata_path(spec)
        tgt = self.get_path_meta_folder(spec)

        tree = LinkTree(src)
        # there should be no conflicts when linking the meta folder
        tree.merge(tgt, link=self.link)

    def print_conflict(self, spec_active, spec_specified, level="error"):
        "Singular print function for spec conflicts."
        cprint = getattr(tty, level)
        color = tty.color.get_color_when()
        linked = tty.color.colorize("   (@gLinked@.)", color=color)
        specified = tty.color.colorize("(@rSpecified@.)", color=color)
        cprint(
            f"{self._croot}Package conflict detected:\n"
            f"{linked} {colorize_spec(spec_active)}\n"
            f"{specified} {colorize_spec(spec_specified)}"
        )

    def print_status(self, *specs, **kwargs):
        if kwargs.get("with_dependencies", False):
            specs = set(get_dependencies(specs))

        specs = sorted(specs, key=lambda s: s.name)
        in_view = list(map(self.get_spec, specs))

        for s, v in zip(specs, in_view):
            if not v:
                tty.error(f"{self._croot}Package not linked: {s.name}")
            elif s != v:
                self.print_conflict(v, s, level="warn")

        in_view = list(filter(None, in_view))

        if len(specs) > 0:
            tty.msg(f"Packages linked in {self._croot[:-1]}:")

            # Make a dict with specs keyed by architecture and compiler.
            index = index_by(specs, ("architecture", "compiler"))

            # Traverse the index and print out each package
            for i, (architecture, compiler) in enumerate(sorted(index)):
                if i > 0:
                    print()

                header = (
                    f"{spack.spec.ARCHITECTURE_COLOR}{{{architecture}}} "
                    f"/ {spack.spec.COMPILER_COLOR}{{{compiler}}}"
                )
                tty.hline(colorize(header), char="-")

                specs = index[(architecture, compiler)]
                specs.sort()

                abbreviated = [
                    s.cformat("{name}{@version}{compiler_flags}{variants}{%compiler}")
                    for s in specs
                ]

                # Print one spec per line along with prefix path
                width = max(len(s) for s in abbreviated)
                width += 2
                format = "    %%-%ds%%s" % width

                for abbrv, s in zip(abbreviated, specs):
                    prefix = ""
                    if self.verbose:
                        prefix = colorize("@K{%s}" % s.dag_hash(7))
                    print(prefix + (format % (abbrv, self.get_projection_for_spec(s))))
        else:
            tty.warn(self._croot + "No packages found.")

    def _purge_empty_directories(self):
        remove_empty_directories(self._root)

    def _purge_broken_links(self):
        remove_dead_links(self._root)

    def clean(self):
        self._purge_broken_links()
        self._purge_empty_directories()

    def unlink_meta_folder(self, spec):
        path = self.get_path_meta_folder(spec)
        assert os.path.exists(path)
        shutil.rmtree(path)


class SimpleFilesystemView(FilesystemView):
    """A simple and partial implementation of FilesystemView focused on performance and immutable
    views, where specs cannot be removed after they were added."""

    def _sanity_check_view_projection(self, specs):
        """A very common issue is that we end up with two specs of the same package, that project
        to the same prefix. We want to catch that as early as possible and give a sensible error to
        the user. Here we use the metadata dir (.spack) projection as a quick test to see whether
        two specs in the view are going to clash. The metadata dir is used because it's always
        added by Spack with identical files, so a guaranteed clash that's easily verified."""
        seen = {}
        for current_spec in specs:
            metadata_dir = self.relative_metadata_dir_for_spec(current_spec)
            conflicting_spec = seen.get(metadata_dir)
            if conflicting_spec:
                raise ConflictingSpecsError(current_spec, conflicting_spec)
            seen[metadata_dir] = current_spec

    def add_specs(self, *specs, **kwargs) -> None:
        """Link a root-to-leaf topologically ordered list of specs into the view."""
        assert all((s.concrete for s in specs))
        if len(specs) == 0:
            return

        # Drop externals
        specs = [s for s in specs if not s.external]

        self._sanity_check_view_projection(specs)

        # Ignore spack meta data folder.
        def skip_list(file):
            return os.path.basename(file) == spack.store.STORE.layout.metadata_dir

        # Determine if the root is on a case-insensitive filesystem
        normalize_paths = is_folder_on_case_insensitive_filesystem(self._destination)

        sources = [
            (spec.package.view_source(), self.get_relative_projection_for_spec(spec))
            for spec in specs
        ]
        visitor = MultiPrefixMerger(
            sources,
            ignore=skip_list,
            normalize_paths=normalize_paths,
            dir_symlink_optimization=self.link_dirs,
        )

        # Check for conflicts in destination dir.
        visit_directory_tree(self._destination, DestinationMergeVisitor(visitor))

        # Throw on fatal dir-file conflicts.
        if visitor.fatal_conflicts:
            raise MergeConflictSummary(visitor.fatal_conflicts)

        # Inform about file-file conflicts.
        if visitor.file_conflicts:
            if self.ignore_conflicts:
                tty.debug(f"{len(visitor.file_conflicts)} file conflicts")
            else:
                raise MergeConflictSummary(visitor.file_conflicts)

        tty.debug(f"Creating {len(visitor.directories)} dirs and {len(visitor.files)} links")

        # Make the directory structure
        for dst in visitor.directories:
            self.mkdir(os.path.join(self._root, dst))

        # Link the files using a "merge map": full src => full dst
        merge_map_per_prefix = self._source_merge_visitor_to_merge_map(visitor)
        for spec in specs:
            merge_map = merge_map_per_prefix.get(spec.package.view_source(), None)
            if not merge_map:
                # Not every spec may have files to contribute.
                continue
            spec.package.add_files_to_view(self, merge_map, skip_if_exists=False)

        # Finally create the metadata dirs.
        self.link_metadata(specs)

    def _source_merge_visitor_to_merge_map(self, visitor: MultiPrefixMerger):
        # For compatibility with add_files_to_view, we have to create a
        # merge_map of the form join(src_root, src_rel) => join(dst_root, dst_rel),
        # but our visitor.files format is dst_rel => (src_root, src_rel).
        merge_map: Dict[str, Dict[str, str]] = {}
        for dst_rel, (src_root, src_rel) in visitor.files.items():
            per_source = merge_map.get(src_root)
            if per_source is None:
                per_source = merge_map[src_root] = {}
            per_source[os.path.join(src_root, src_rel)] = os.path.join(self._root, dst_rel)
        return merge_map

    def relative_metadata_dir_for_spec(self, spec):
        return os.path.join(
            self.get_relative_projection_for_spec(spec),
            spack.store.STORE.layout.metadata_dir,
            spec.name,
        )

    def link_metadata(self, specs):
        prefix_and_projection = [
            (
                os.path.join(spec.package.view_source(), spack.store.STORE.layout.metadata_dir),
                self.relative_metadata_dir_for_spec(spec),
            )
            for spec in specs
        ]
        metadata_visitor = MultiPrefixMerger(prefix_and_projection)

        # Check for conflicts in destination dir.
        visit_directory_tree(self._destination, DestinationMergeVisitor(metadata_visitor))

        # Throw on dir-file conflicts -- unlikely, but who knows.
        if metadata_visitor.fatal_conflicts:
            raise MergeConflictSummary(metadata_visitor.fatal_conflicts)

        # We are strict here for historical reasons
        if metadata_visitor.file_conflicts:
            raise MergeConflictSummary(metadata_visitor.file_conflicts)

        for dst in metadata_visitor.directories:
            self.mkdir(os.path.join(self._root, dst))

        for dst_relpath, (src_root, src_relpath) in metadata_visitor.files.items():
            self.link(os.path.join(src_root, src_relpath), os.path.join(self._root, dst_relpath))

    def get_relative_projection_for_spec(self, spec):
        # Extensions are placed by their extendee, not by their own spec
        if spec.package.extendee_spec:
            spec = spec.package.extendee_spec

        p = spack.projections.get_projection(self.projections, spec)
        return spec.format_path(p) if p else ""

    def get_projection_for_spec(self, spec):
        """
        Return the projection for a spec in this view.

        Relies on the ordering of projections to avoid ambiguity.
        """
        spec = spack.spec.Spec(spec)

        if spec.package.extendee_spec:
            spec = spec.package.extendee_spec

        proj = spack.projections.get_projection(self.projections, spec)
        if proj:
            return os.path.join(self._root, spec.format_path(proj))
        return self._root


#####################
# utility functions #
#####################
def get_spec_from_file(filename) -> Optional[spack.spec.Spec]:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return spack.spec.Spec.from_yaml(f)
    except OSError:
        return None


def colorize_root(root):
    colorize = ft.partial(tty.color.colorize, color=tty.color.get_color_when())
    pre, post = map(colorize, "@M[@. @M]@.".split())
    return f"{pre}{root}{post}"


def colorize_spec(spec):
    "Colorize spec output unless colors are turned off."
    if tty.color.get_color_when():
        return spec.cshort_spec
    else:
        return spec.short_spec


def find_dependents(all_specs, providers, deptype="run"):
    """
    Return a set containing all those specs from all_specs that depend on
    providers at the given dependency type.
    """
    dependents = set()
    for s in all_specs:
        for dep in s.traverse(deptype=deptype):
            if dep in providers:
                dependents.add(s)
    return dependents


def filter_exclude(specs, exclude):
    "Filter specs given sequence of exclude regex"
    to_exclude = [re.compile(e) for e in exclude]

    def keep(spec):
        for e in to_exclude:
            if e.match(spec.name):
                return False
        return True

    return filter(keep, specs)


def get_dependencies(specs):
    "Get set of dependencies (includes specs)"
    retval = set()
    set(map(retval.update, (set(s.traverse()) for s in specs)))
    return retval


class ConflictingProjectionsError(SpackError):
    """Raised when a view has a projections file and is given one manually."""


def is_folder_on_case_insensitive_filesystem(path: str) -> bool:
    with tempfile.NamedTemporaryFile(dir=path, prefix=".sentinel") as sentinel:
        return os.path.exists(os.path.join(path, os.path.basename(sentinel.name).upper()))
