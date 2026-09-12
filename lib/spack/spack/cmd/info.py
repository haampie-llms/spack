# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
# mypy: disallow-untyped-defs

"""``spack info``: show what a package recipe declares.

The command has two layouts, chosen from whether stdout is a terminal:

* On a terminal, entries are laid out in aligned columns, long text is wrapped to the terminal
  width, and an entry with a lot of detail (a variant with a ``when`` condition and a list of
  allowed values, say) spans several lines.
* In a pipe, every entry is exactly one line and nothing is wrapped, so ``grep`` and other tools
  see each fact together with the name it belongs to.

Both layouts show the same information in the same order.
"""

import argparse
import collections
import io
import json
import re
import shutil
import sys
from argparse import Namespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, TextIO, Tuple, Union

import spack.builder
import spack.cmd
import spack.config
import spack.dependency
import spack.deptypes as dt
import spack.fetch_strategy as fs
import spack.install_test
import spack.package_base
import spack.repo
import spack.spec
import spack.store
import spack.variant
import spack.version
from spack.cmd.common import arguments
from spack.package_base import PackageBase
from spack.util.tty import color
from spack.util.tty.colify import colify

description = "get detailed information on a particular package"
section = "query"
level = "short"

#: color of section titles and labels
HEADER_COLOR = "@*b"
#: color of the ``when`` keyword in front of conditions
WHEN_COLOR = "@*"

#: Indentation of entries under a section title
INDENT = 4
#: Blanks between columns
GUTTER = 2
#: Never squeeze the text column (descriptions, conditions) narrower than this on a terminal
MIN_TEXT_WIDTH = 30
#: In a pipe, do not pad the name column beyond this for the sake of a few overlong names
MAX_NAME_WIDTH_IN_PIPE = 48
#: Placeholder for a variant value that is forwarded from the package to a dependency, e.g. in
#: ``kokkos cuda_arch=* when cuda_arch=*``
PLACEHOLDER = "*"
#: Language virtuals, listed in the label block rather than among the dependencies
LANGUAGES = ("c", "cxx", "fortran")


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "-a",
        "--all",
        action="store_true",
        default=False,
        help="output all package information, including download URLs for every version",
    )

    by = subparser.add_mutually_exclusive_group()
    by.add_argument(
        "--by-name",
        dest="by_name",
        action="store_true",
        default=True,
        help="list variants, dependency, etc. in name order, then by when condition",
    )
    by.add_argument(
        "--by-when",
        dest="by_name",
        action="store_false",
        default=False,
        help="group variants, dependencies, etc. first by when condition, then by name",
    )

    subparser.add_argument(
        "--json",
        action="store_true",
        help="output everything as JSON (versions, variants, dependencies, conflicts, patches, "
        "local state); conditions are spec strings, dependencies are not merged",
    )

    options = [
        ("--conflicts", print_conflicts.__doc__),
        ("--detectable", print_detectable.__doc__),
        ("--maintainers", print_maintainers.__doc__),
        ("--namespace", print_namespace.__doc__),
        ("--no-dependencies", f"do not {print_dependencies.__doc__}"),
        ("--no-variants", f"do not {print_variants.__doc__}"),
        ("--no-versions", f"do not {print_versions.__doc__}"),
        ("--patches", print_patches.__doc__),
        ("--phases", print_phases.__doc__),
        ("--tags", print_tags.__doc__),
        ("--tests", print_tests.__doc__),
        ("--virtuals", print_virtuals.__doc__),
    ]
    for opt, help_comment in options:
        subparser.add_argument(opt, action="store_true", help=help_comment)

    # deprecated for the more generic --by-name, but still here until we can remove it
    subparser.add_argument(
        "--variants-by-name",
        dest="by_name",
        action=arguments.DeprecatedStoreTrueAction,
        help=argparse.SUPPRESS,
        removed_in="a future Spack release",
        instructions="use --by-name instead",
    )
    arguments.add_common_arguments(subparser, ["spec"])


#: Whether stdout is a terminal. A function so that tests can pretend either way.
def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class Layout:
    """How entries are laid out: ``tty`` selects the multi-line terminal layout over the
    one-line-per-entry layout for pipes; ``width`` is the wrapping width (``None``: no wrapping).
    """

    __slots__ = ("tty", "width")

    def __init__(self, tty: bool, width: Optional[int]) -> None:
        self.tty = tty
        self.width = width

    @staticmethod
    def detect() -> "Layout":
        if _stdout_is_tty():
            return Layout(tty=True, width=shutil.get_terminal_size().columns)
        return Layout(tty=False, width=None)


class Entry:
    """One thing to list under a section: a variant, a dependency, a license, ...

    Attributes:
        name: what the entry is about, e.g. ``cuda_arch [none]`` or ``kokkos+cuda`` (colorized)
        head: leading part of the plain ``name`` shared by consecutive entries about the same
            thing (e.g. the ``kokkos`` in ``kokkos+cuda``); on a terminal it is shown only once
        attr: short attribute shown in its own column, e.g. dependency types (colorized)
        description: free text, wrapped on a terminal
        when: condition under which the entry applies, if any
        when_text: how the condition is displayed (colorized; may contain placeholders)
        extra: further lines of detail, e.g. the allowed values of a variant (colorized)
        sort_key: entries of a section are listed in this order
        spec: the spec behind ``name``, if it is one (a dependency, a conflict), so that entries
            differing in one part of it can be merged
    """

    __slots__ = (
        "name",
        "head",
        "attr",
        "description",
        "when",
        "when_text",
        "extra",
        "sort_key",
        "spec",
        "merged",
        "relevant",
    )

    def __init__(
        self,
        name: str,
        *,
        head: str = "",
        attr: str = "",
        description: str = "",
        when: Optional[spack.spec.Spec] = None,
        when_text: Optional[str] = None,
        extra: Sequence[str] = (),
        sort_key: Any = None,
        spec: Optional[spack.spec.Spec] = None,
    ) -> None:
        self.name = name
        self.spec = spec
        self.merged = False
        #: whether the entry can apply to the version the spec resolves to (see `mark_relevance`)
        self.relevant = True
        self.head = head
        self.attr = attr
        self.description = description
        self.when = when if when is not None and when != spack.spec.Spec() else None
        self.when_text = when_text if when_text is not None else _spec_text(self.when)
        self.extra = list(extra)
        self.sort_key = sort_key

    @property
    def when_key(self) -> str:
        """Plain text of the condition, for grouping and sorting."""
        return color.csub(self.when_text)

    def fields(self, show_when: bool) -> List[Tuple[str, str]]:
        """The text fields after the name and attribute columns, in display order, as (lead,
        text) pairs: the lead (``when``) is never separated from the text by wrapping."""
        result = []
        if self.description:
            result.append(("", self.description))
        if show_when and self.when_text:
            result.append((color.colorize(f"{WHEN_COLOR}{{when}} "), self.when_text))
        result.extend(("", extra) for extra in self.extra)
        return result


def _spec_text(spec: Optional[spack.spec.Spec]) -> str:
    """Colorized (if enabled) string for a spec, empty for no spec or an empty spec."""
    if spec is None or spec == spack.spec.Spec():
        return ""
    return spec.clong_spec


def _faint(text: str) -> str:
    """Render text faint (ANSI SGR 2), dropping any other colors, if color is enabled."""
    if not color.get_color_when():
        return text
    return f"\x1b[2m{color.csub(text)}\x1b[0m"


def _title(text: str) -> str:
    return color.colorize(f"{HEADER_COLOR}{{{color.cescape(text)}}}")


def _ljust(text: str, width: int) -> str:
    """Left-justify text that may contain color codes."""
    return text + " " * max(0, width - color.clen(text))


def _wrap(text: str, first_indent: str, indent: str, width: Optional[int]) -> List[str]:
    """Wrap ``text`` to ``width``, honoring explicit line breaks. ``first_indent`` may contain
    color codes; it is what the first line starts with, later lines start with ``indent``."""
    lines: List[str] = []
    for i, paragraph in enumerate(text.split("\n")):
        if i > 0:
            first_indent = indent
        if width is None:
            lines.append(first_indent + paragraph)
            continue
        # textwrap counts color codes in the indent, so wrap with blanks of the same visible
        # width and put the real prefix back afterwards
        blanks = " " * color.clen(first_indent)
        wrapped = color.cwrap(
            paragraph,
            width=max(width - 1, color.clen(indent) + MIN_TEXT_WIDTH),
            initial_indent=blanks,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [blanks]
        wrapped[0] = first_indent + wrapped[0][len(blanks) :]
        lines.extend(wrapped)
    return lines


class EntryPrinter:
    """Prints entries in aligned columns: name, attribute (optional), then text fields.

    Column widths are computed over all entries passed at construction so that groups printed
    separately (e.g. per ``when`` condition) still line up.
    """

    def __init__(
        self,
        entries: Iterable[Entry],
        layout: Layout,
        indent: int = INDENT,
        out: Optional[TextIO] = None,
        irrelevant_note: str = "",
    ) -> None:
        entries = list(entries)
        self.layout = layout
        self.indent = indent
        self.out = out or sys.stdout
        #: appended to entries that cannot apply to the version the spec resolves to (in a pipe;
        #: on a terminal those entries are rendered faint instead)
        self.irrelevant_note = irrelevant_note
        self.name_width = max((color.clen(e.name) for e in entries), default=0)
        self.attr_width = max((color.clen(e.attr) for e in entries), default=0)
        if layout.width is not None:
            # leave room for the text column, at the expense of overlong names
            available = layout.width - indent - GUTTER - MIN_TEXT_WIDTH
            if self.attr_width:
                available -= self.attr_width + GUTTER
            self.name_width = max(min(self.name_width, available), 1)
        else:
            self.name_width = min(self.name_width, MAX_NAME_WIDTH_IN_PIPE)

    def print(self, entries: Sequence[Entry], show_when: bool = True) -> None:
        previous_head: Optional[str] = None
        for entry in entries:
            name = entry.name
            plain = color.csub(name)
            # e.g. print `kokkos` once for a run of `kokkos@...`, `kokkos+cuda`, ... entries
            if (
                self.layout.tty
                and entry.head
                and entry.head == previous_head
                and name.startswith(entry.head)  # colors start after the head
                and plain != entry.head
            ):
                name = " " * len(entry.head) + name[len(entry.head) :]
            previous_head = entry.head

            fields = entry.fields(show_when)
            if self.layout.tty:
                self._print_tty(name, entry.attr, fields, faint=not entry.relevant)
            else:
                if not entry.relevant and self.irrelevant_note:
                    fields.append(("", self.irrelevant_note))
                self._print_line(name, entry.attr, fields)

    def _print_line(self, name: str, attr: str, fields: List[Tuple[str, str]]) -> None:
        """One line per entry. Only a description with explicit line breaks (detailed help for a
        variant, say) continues on further lines, indented; the first line still carries the
        condition and the values."""
        columns = [" " * self.indent + _ljust(name, self.name_width)]
        if self.attr_width:
            columns.append(_ljust(attr, self.attr_width))
        continuation: List[str] = []
        for i, (lead, field) in enumerate(fields):
            lines = [" ".join(line.split()) for line in field.split("\n")]
            columns.append(lead + lines[0])
            if i == 0:
                continuation = lines[1:]
            else:
                columns[-1] = lead + " ".join(lines)
        text_column = self.indent + self.name_width + GUTTER
        if self.attr_width:
            text_column += self.attr_width + GUTTER
        self.out.write((" " * GUTTER).join(columns).rstrip() + "\n")
        for line in continuation:
            self.out.write(" " * text_column + line + "\n")

    def _print_tty(
        self, name: str, attr: str, fields: List[Tuple[str, str]], faint: bool = False
    ) -> None:
        """Name and attribute in their columns, then the fields one below the other, wrapped."""
        out = self.out
        if faint:
            out = io.StringIO()
        text_column = self.indent + self.name_width + GUTTER
        if self.attr_width:
            text_column += self.attr_width + GUTTER

        prefix = " " * self.indent + _ljust(name, self.name_width) + " " * GUTTER
        if color.clen(name) > self.name_width:
            # overlong name: continue on the next line at the attribute column
            out.write(prefix.rstrip() + "\n")
            prefix = " " * (self.indent + self.name_width + GUTTER)
        if self.attr_width:
            prefix += _ljust(attr, self.attr_width) + " " * GUTTER

        if not fields:
            out.write(prefix.rstrip() + "\n")
        else:
            indent = " " * text_column
            for i, (lead, field) in enumerate(fields):
                first_indent = (prefix if i == 0 else indent) + lead
                for line in _wrap(field, first_indent, indent, self.layout.width):
                    out.write(line.rstrip() + "\n")

        if faint:
            assert isinstance(out, io.StringIO)
            for line in out.getvalue().splitlines():
                self.out.write(_faint(line) + "\n")


def mark_relevance(entries: List[Entry], version: Optional[spack.version.StandardVersion]) -> None:
    """Flag entries whose condition rules out the version the spec resolves to. They are shown
    faint on a terminal and tagged in a pipe, so that a reader sees what applies to what they
    would get without losing the rest."""
    if version is None:
        return
    resolved = spack.spec.Spec(f"@={version}")
    for entry in entries:
        entry.relevant = entry.when is None or entry.when.intersects(resolved)


def print_section(title: str, entries: List[Entry], args: Namespace) -> None:
    """Print a titled section of entries, either in name order with their conditions inline, or
    grouped by condition (unconditional entries first)."""
    layout, by_name = args.layout, args.by_name
    print()
    print(_title(f"{title}:"))
    if not entries:
        print(" " * INDENT + "None")
        return

    mark_relevance(entries, args.resolved_version)
    entries = merge_conditions(entries)
    note = f"(not for @{args.resolved_version})" if args.resolved_version else ""
    printer = EntryPrinter(entries, layout, irrelevant_note=note)
    if by_name:
        printer.print(entries, show_when=True)
        return

    groups: Dict[str, List[Entry]] = collections.OrderedDict()
    for entry in sorted(entries, key=lambda e: (e.when_key != "", e.when_key)):
        groups.setdefault(entry.when_key, []).append(entry)

    for i, (when_key, group) in enumerate(groups.items()):
        if when_key:
            if i > 0:
                print()
            when = color.colorize(f"{WHEN_COLOR}{{when}} ") + group[0].when_text
            if layout.tty and not any(e.relevant for e in group):
                when = _faint(when)
            print(" " * (INDENT - 2) + when)
        printer.print(group, show_when=False)


def print_labeled(rows: List[Tuple[str, str]]) -> None:
    """Print ``Label:  value`` rows with the values aligned. Multi-line values continue under the
    first line of the value."""
    if not rows:
        return
    width = max(len(label) for label, _ in rows) + 1  # for the colon
    for label, value in rows:
        lines = value.split("\n") or [""]
        print(f"{_ljust(_title(label + ':'), width)}{' ' * GUTTER}{lines[0]}".rstrip())
        for line in lines[1:]:
            print(" " * (width + GUTTER) + line)


def _deptypes(depflag: int) -> str:
    color_flags = zip("gcbm", dt.ALL_FLAGS)
    return ", ".join(
        color.colorize(f"@{c}{{{dt.flag_to_string(depflag & flag)}}}")
        for c, flag in color_flags
        if depflag & flag
    )


# --- Dependencies -------------------------------------------------------------------------------


def _forwarded_variants(when: spack.spec.Spec, dep: spack.spec.Spec) -> List[str]:
    """Names of single-valued variants that a dependency receives from the package unchanged,
    as in ``depends_on("kokkos cuda_arch=80", when="cuda_arch=80")``."""
    result = []
    for name, condition in when.variants.items():
        forwarded = dep.variants.get(name)
        if (
            forwarded is not None
            and condition.type != spack.variant.VariantType.BOOL
            and forwarded.type != spack.variant.VariantType.BOOL
            and not condition.propagate
            and not forwarded.propagate
            and len(condition.values) == 1
            and condition.values == forwarded.values
        ):
            result.append(name)
    return result


#: characters that make up one component of a spec string (a version range, a `name=value`
#: variant, a `%dep@version` ...): a component ends where one of these does not follow
_COMPONENT_CHARS = r"A-Za-z0-9_.:,'\-"


def _replace_component(text: str, needle: str, replacement: str, after_name: bool = False) -> str:
    """Replace one rendered component of a spec (``cuda_arch=10``, ``@1.8``, ``%c``, ...) in
    possibly colorized spec text, where it is preceded by a blank, a color code or the start of
    the text and not followed by more of the same component (so that ``cuda_arch=10`` does not
    match inside ``cuda_arch=100``). With ``after_name``, the component may also directly follow
    a package name, as the version range in ``mpich@3:`` does."""
    # a boolean variant (+x, ~x) may also directly follow a version range
    prefixes = [r"^", r"\s", r"\x1b\[[0-9;]*m", r"(?=[+~])"]
    if after_name:
        prefixes.append(r"(?<=[A-Za-z0-9_.-])(?=@)")
    pattern = rf"({'|'.join(prefixes)}){re.escape(needle)}(?![{_COMPONENT_CHARS}])"
    return re.sub(pattern, lambda m: m.group(1) + replacement, text)


def _condition_parts(when: spack.spec.Spec) -> Dict[str, str]:
    """Split a spec into its components, keyed by what they constrain, each rendered as it
    appears in the plain string of the spec. Boolean variants form one component (``+a~b``)."""
    parts: Dict[str, str] = {}
    if when.name:
        parts["package"] = when.name
    if when.versions != spack.version.any_version:
        parts["version"] = when.format("{@versions}")
    bools = []
    for name in sorted(when.variants):
        value = when.variants[name]
        if value.type == spack.variant.VariantType.BOOL:
            bools.append(str(value))
        else:
            parts[f"variant:{name}"] = str(value)
    if bools:
        parts["bools"] = "".join(bools)
    if when.architecture is not None:
        for attr in ("platform", "os", "target"):
            value = getattr(when.architecture, attr)
            if value is not None:
                parts[attr] = f"{attr}={value}"
    if when.compiler_flags:
        parts["flags"] = when.format("{compiler_flags}").strip()
    deps = when._format_dependencies(color=False)
    if deps:
        parts["deps"] = deps
    return parts


def _braces(values: List[str]) -> str:
    """Write alternatives compactly, e.g. ``cuda_arch=80`` and ``cuda_arch=90`` as
    ``cuda_arch={80,90}``, ``%c`` and ``%cxx`` as ``%{c,cxx}``, ``@1.8`` and ``@1.9`` as
    ``@{1.8,1.9}``."""
    prefix = values[0]
    for value in values[1:]:
        while not value.startswith(prefix):
            prefix = prefix[:-1]
    # cut the shared prefix back to a separator so that gfx1010/gfx1011 don't become gfx101{0,1}
    cut = max(prefix.rfind(c) for c in "=@%^+~")
    prefix = prefix[: cut + 1] if cut >= 0 else ""
    return prefix + "{" + ",".join(v[len(prefix) :] for v in values) + "}"


def _entry_parts(entry: Entry) -> Dict[str, str]:
    """Components of an entry's name (if it is a spec) and of its condition."""
    parts: Dict[str, str] = {}
    if entry.spec is not None:
        parts.update((f"name:{k}", v) for k, v in _condition_parts(entry.spec).items())
    else:
        parts["name"] = color.csub(entry.name)
    if entry.when is not None:
        parts.update((f"when:{k}", v) for k, v in _condition_parts(entry.when).items())
    return parts


def _bool_names(bools: str) -> List[str]:
    return re.split(r"[+~]", bools)[1:]


#: A dependency alternative that is simple enough to be listed in braces: one `%name@version`
#: or `^name@version`, nothing more
_SIMPLE_DEP = re.compile(r"^[%^][A-Za-z0-9_.-]+(@[A-Za-z0-9_.:,=-]+)?$")


class _MergedCondition:
    """Entries whose name or condition differ in exactly one component, being merged into one."""

    __slots__ = ("entries", "parts", "varying", "values")

    def __init__(self, entry: Entry, parts: Dict[str, str]) -> None:
        self.entries = [entry]
        self.parts = parts
        self.varying: Optional[str] = None
        self.values = [parts]

    def absorb(self, entry: Entry, parts: Dict[str, str]) -> bool:
        if parts.keys() != self.parts.keys():
            return False
        differing = [k for k in parts if parts[k] != self.parts[k]]
        if len(differing) != 1 or (self.varying is not None and differing[0] != self.varying):
            return False
        key = differing[0]
        # the dependency name is what a reader looks up: never fold two packages into one row
        if key in ("name", "name:package"):
            return False
        # +x and ~x of the same variants are not alternatives of one condition, but its absence
        if key.endswith("bools") and _bool_names(parts[key]) == _bool_names(self.parts[key]):
            return False
        # %cuda@:11.0.2~foo target=ppc64le: and %cuda@:11.0.3~foo target=x86_64: in braces would
        # be unreadable; only merge simple alternatives like %clang@11 and %clang@12
        if key.endswith("deps") and not all(
            _SIMPLE_DEP.match(v) for v in (parts[key], self.parts[key])
        ):
            return False
        self.varying = key
        self.values.append(parts)
        self.entries.append(entry)
        return True

    def result(self) -> List[Entry]:
        """The merged entry, or the original entries if the merge cannot be rendered."""
        first = self.entries[0]
        if self.varying is None:
            return [first]
        needle = self.parts[self.varying]
        alternatives = _braces([parts[self.varying] for parts in self.values])
        text = first.name if self.varying.startswith("name:") else first.when_text
        replaced = _replace_component(
            text, needle, alternatives, after_name=self.varying == "name:version"
        )
        if replaced == text:  # the component was not found as rendered: don't hide anything
            return self.entries
        if self.varying.startswith("name:"):
            first.name = replaced
        else:
            first.when_text = replaced
        first.relevant = any(e.relevant for e in self.entries)
        return [first]


def merge_conditions(entries: List[Entry]) -> List[Entry]:
    """Merge entries that differ in one component of their name or condition only, e.g.
    ``cuda@12.9: when cuda_arch=103`` and ``cuda@12.9: when cuda_arch=103a`` into
    ``cuda@12.9: when cuda_arch={103,103a}``, or the conflicts ``+amesos when ~epetra`` and
    ``+aztec when ~epetra`` into ``+{amesos,aztec} when ~epetra``. Order is that of the first
    entry of each merge."""
    merges: Dict[Tuple, List[_MergedCondition]] = {}
    result: List[Union[Entry, _MergedCondition]] = []
    for entry in entries:
        if (
            entry.merged
            or PLACEHOLDER in entry.when_key
            or (entry.when is None and entry.spec is None)
        ):
            result.append(entry)
            continue
        key = (color.csub(entry.attr), entry.description, tuple(entry.extra))
        parts = _entry_parts(entry)
        for merge in merges.get(key, []):
            if merge.absorb(entry, parts):
                break
        else:
            merge = _MergedCondition(entry, parts)
            merges.setdefault(key, []).append(merge)
            result.append(merge)
    flat: List[Entry] = []
    for item in result:
        flat.extend(item.result() if isinstance(item, _MergedCondition) else [item])
    for entry in flat:
        entry.merged = True
    return flat


def _placeholder_legend(pkg: PackageBase, variant: str, values: List[str]) -> str:
    """Explain what ``*`` stands for in a collapsed group of forwarded dependencies."""
    possible: List[str] = []
    for _, definition in pkg.variant_definitions(variant):
        possible.extend(str(v) for v in definition.possible_values() or ())
    possible = [v for v in possible if v != "none"]

    seen = set(values)
    if possible and seen >= set(possible):
        which = f"any of the {len(possible)} {variant} values"
    elif len(values) <= 10:
        which = f"one of {variant}={', '.join(values)}"
    else:
        which = f"one of {len(values)} {variant} values"
    return color.colorize(f"({PLACEHOLDER} = {color.cescape(which)})")


def _natural(text: str) -> Tuple:
    """Sort key that orders embedded numbers numerically: cuda_arch=90 before cuda_arch=100."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text))


def _dependencies_by_name(
    pkg: PackageBase,
) -> Dict[str, List[Tuple[spack.spec.Spec, spack.dependency.Dependency]]]:
    """The dependencies possible for the package's spec, grouped by dependency name."""
    # unlike variants, dependency declarations never override each other: every one applies
    # whenever its condition holds, so all of them are shown
    by_name: Dict[str, List[Tuple[spack.spec.Spec, spack.dependency.Dependency]]] = {}
    for when, deps in pkg.dependencies.items():
        if not pkg.intersects(when):
            continue
        for name, dep in deps.items():
            by_name.setdefault(name, []).append((when, dep))
    return by_name


def _dependency_text(spec: spack.spec.Spec) -> str:
    """The dependency spec, marked if it is a virtual package (provided by others)."""
    text = _spec_text(spec)
    if spack.repo.PATH.is_virtual(spec.name):
        text += " (virtual)"
    return text


def print_languages(pkg: PackageBase, args: Namespace) -> None:
    """output the languages the package is written in, with conditions"""
    languages = []
    for name, deps in _dependencies_by_name(pkg).items():
        if name not in LANGUAGES:
            continue
        conditions = [_spec_text(when) for when, _ in deps if when != spack.spec.Spec()]
        if conditions and len(conditions) == len(deps):
            languages.append(f"{name} (when {' or '.join(conditions)})")
        else:
            languages.append(name)
    if languages:
        args.rows.append(("Languages", ", ".join(languages)))


def dependency_entries(pkg: PackageBase) -> List[Entry]:
    """Entries for the dependencies of ``pkg`` that are possible for its spec, languages aside.

    Runs of dependencies that only forward a variant value, e.g. ``kokkos cuda_arch=X`` when
    ``cuda_arch=X`` for every ``X``, are collapsed into a single entry with a placeholder.
    """
    by_name = {
        name: deps for name, deps in _dependencies_by_name(pkg).items() if name not in LANGUAGES
    }
    entries: List[Entry] = []
    for name in sorted(by_name):
        # group (when, dependency) pairs by what they look like with forwarded values replaced
        Group = List[Tuple[spack.spec.Spec, spack.dependency.Dependency]]
        groups: Dict[Tuple[str, str, int], Group] = collections.OrderedDict()
        forwarded_by_group: Dict[Tuple[str, str, int], List[str]] = {}
        for when, dep in by_name[name]:
            forwarded = _forwarded_variants(when, dep.spec)
            dep_key, when_key = dep.spec.long_spec, when.long_spec
            for variant in forwarded:
                needle = when.format(f"{{variants.{variant}}}")
                dep_key = _replace_component(dep_key, needle, f"{variant}={PLACEHOLDER}")
                when_key = _replace_component(when_key, needle, f"{variant}={PLACEHOLDER}")
            key = (dep_key, when_key, dep.depflag)
            groups.setdefault(key, []).append((when, dep))
            forwarded_by_group[key] = forwarded

        def sort_key(when: spack.spec.Spec, dep: spack.dependency.Dependency) -> Tuple:
            # unconstrained and unconditional first; then keep conditions of the same shape
            # together (e.g. all `when cuda_arch=...` rows), ordered by version constraint
            return (
                name,
                dep.spec != spack.spec.Spec(name),
                when != spack.spec.Spec(),
                sorted(when.variants),
                dep.spec.versions,
                _natural(dep.spec.long_spec),
            )

        for key, group in groups.items():
            when, dep = group[0]
            forwarded = forwarded_by_group[key]
            if len(group) < 2 or not forwarded:
                for when, dep in group:
                    entries.append(
                        Entry(
                            _dependency_text(dep.spec),
                            head=name,
                            attr=_deptypes(dep.depflag),
                            when=when,
                            spec=dep.spec,
                            sort_key=sort_key(when, dep) + (_natural(when.long_spec),),
                        )
                    )
                continue

            dep_text, when_text = _dependency_text(dep.spec), _spec_text(when)
            legend = []
            for variant in forwarded:
                needle = when.format(f"{{variants.{variant}}}")
                replacement = f"{variant}={PLACEHOLDER}"
                dep_text = _replace_component(dep_text, needle, replacement)
                when_text = _replace_component(when_text, needle, replacement)
                values = [str(w.variants[variant].values[0]) for w, _ in group]
                legend.append(_placeholder_legend(pkg, variant, values))
            entries.append(
                Entry(
                    dep_text,
                    head=name,
                    attr=_deptypes(dep.depflag),
                    when=when,
                    when_text=when_text,
                    extra=legend,
                    sort_key=sort_key(when, dep) + (_natural(key[1]),),
                )
            )

    entries.sort(key=lambda e: e.sort_key)
    return entries


def print_dependencies(pkg: PackageBase, args: Namespace) -> None:
    """output build, link, and run package dependencies"""
    print_section("Dependencies", dependency_entries(pkg), args)


# --- Conflicts, requirements and patches --------------------------------------------------------


def _message(pkg: PackageBase, msg: Optional[str]) -> str:
    """Directive messages are stored prefixed with the package name; drop it."""
    if not msg:
        return ""
    prefix = f"{pkg.name}: "
    return msg[len(prefix) :] if msg.startswith(prefix) else msg


def conflict_entries(pkg: PackageBase) -> List[Entry]:
    entries = []
    for when, conflicts in pkg.conflicts.items():
        if not pkg.intersects(when):
            continue
        for spec, msg in conflicts:
            entries.append(
                Entry(_spec_text(spec), spec=spec, when=when, description=_message(pkg, msg))
            )
    entries.sort(key=lambda e: (_natural(color.csub(e.name)), _natural(e.when_key)))
    return entries


def requirement_entries(pkg: PackageBase) -> List[Entry]:
    entries = []
    for when, requirements in pkg.requirements.items():
        if not pkg.intersects(when):
            continue
        for specs, policy, msg in requirements:
            if len(specs) == 1:
                name, spec = _spec_text(specs[0]), specs[0]
            else:
                kind = "one of" if policy == "one_of" else "any of"
                name, spec = f"{kind}: " + ", ".join(_spec_text(s) for s in specs), None
            entries.append(Entry(name, spec=spec, when=when, description=_message(pkg, msg)))
    entries.sort(key=lambda e: (_natural(color.csub(e.name)), _natural(e.when_key)))
    return entries


def patch_entries(pkg: PackageBase) -> List[Entry]:
    """Patches in the order they are declared, which is the order they are applied in."""
    applicable = [
        (patch, when)
        for when, patches in pkg.patches.items()
        if pkg.intersects(when)
        for patch in patches
    ]
    applicable.sort(key=lambda pw: pw[0].ordering_key)
    entries = []
    for patch, when in applicable:
        name = str(getattr(patch, "relative_path", None) or getattr(patch, "url", ""))
        extra = []
        if patch.level != 1:
            extra.append(f"-p{patch.level}")
        if patch.working_dir and patch.working_dir != ".":
            extra.append(f"in {patch.working_dir}")
        entries.append(Entry(color.cescape(name), when=when, extra=extra))
    return entries


def print_conflicts(pkg: PackageBase, args: Namespace) -> None:
    """output conflicts and requirements"""
    print_section("Conflicts", conflict_entries(pkg), args)
    requirements = requirement_entries(pkg)
    if requirements:
        print_section("Requirements", requirements, args)


def print_patches(pkg: PackageBase, args: Namespace) -> None:
    """output patches"""
    print_section("Patches", patch_entries(pkg), args)


def print_counts(pkg: PackageBase, args: Namespace) -> None:
    """Mention how many conflicts, requirements and patches there are when they are not listed."""
    if not (args.all or args.conflicts):
        counts = []
        if pkg.conflicts:
            n = len(conflict_entries(pkg))
            counts.append(f"{n} conflict{'s' if n != 1 else ''}")
        if pkg.requirements:
            n = len(requirement_entries(pkg))
            counts.append(f"{n} requirement{'s' if n != 1 else ''}")
        if counts:
            args.rows.append(("Constraints", ", ".join(counts) + "  (list with --conflicts)"))
    if not (args.all or args.patches) and pkg.patches:
        n = len(patch_entries(pkg))
        args.rows.append(("Patches", f"{n}  (list with --patches)"))


# --- Variants -----------------------------------------------------------------------------------


def _variant_value(v: Any) -> str:
    return str(v).lower() if v is None or isinstance(v, bool) else str(v)


def _variant_values(variant: spack.variant.Variant) -> str:
    """The allowed values of a non-boolean variant, each with its condition if it has one, e.g.
    ``one of: 98, 11, 14, 17 (when @1.63.0:)``. Empty for boolean variants and for variants whose
    values are checked by a validator function."""
    if variant.values is None:
        return ""
    rendered = []
    for value in variant.values:
        condition = None
        if isinstance(value, spack.variant.ConditionalValue):
            if value.when is None:  # statically disabled
                continue
            value, condition = value.value, value.when
        text = color.colorize(f"@c{{{color.cescape(_variant_value(value))}}}")
        if condition is not None and condition != spack.spec.Spec():
            text += f" (when {_spec_text(condition)})"
        rendered.append(text)
    if len(rendered) < 2 or all(isinstance(v, bool) for v in variant.values):
        return ""
    kind = "any of" if variant.multi else "one of"
    return f"{kind}: " + ", ".join(rendered)


def variant_entries(pkg: PackageBase) -> List[Entry]:
    entries = []
    for name in spack.package_base._subkeys(pkg.variants):
        for when, variant in spack.package_base._definitions(pkg.variants, name):
            if not pkg.intersects(when):
                continue
            default = color.cescape(_variant_value(variant.default))
            extra = []
            if variant.sticky:
                extra.append("sticky (only changes when set explicitly)")
            values = _variant_values(variant)
            if values:
                extra.append(values)
            entries.append(
                Entry(
                    color.colorize(f"@c{{{color.cescape(name)}}} @C{{[{default}]}}"),
                    description=variant.description,
                    when=when,
                    extra=extra,
                )
            )
    return entries


def print_variants(pkg: PackageBase, args: Namespace) -> None:
    """output variants"""
    print_section("Variants", variant_entries(pkg), args)


# --- Versions -----------------------------------------------------------------------------------


def _version_text(v: Any) -> str:
    return color.colorize(f"{spack.spec.VERSION_COLOR}{{{color.cescape(str(v))}}}")


def _versions(pkg: PackageBase) -> List[spack.version.StandardVersion]:
    """The package's versions that match its spec, newest first."""
    return sorted((v for v in pkg.versions if pkg.spec.versions.intersects(v)), reverse=True)


def resolved_version(pkg: PackageBase) -> Optional[spack.version.StandardVersion]:
    """The version that the package's spec would resolve to according to the recipe alone (no
    preferences from configuration): the preferred one among the versions matching the spec."""
    versions = _versions(pkg)
    if not versions:
        return None

    def order(version: spack.version.StandardVersion) -> Tuple:
        info = pkg.versions[version]
        not_deprecated = not info.get("deprecated", False)
        return (not_deprecated, *spack.package_base.concretization_version_order((version, info)))

    return max(versions, key=order)


def _url_for(pkg: PackageBase, version: spack.version.StandardVersion) -> str:
    """Where a version is fetched from, as the fetcher describes it."""
    if not pkg.has_code:
        return ""
    try:
        return str(spack.package_base.for_package_version(pkg, version))
    except fs.InvalidArgsError:
        return "No URL"


def print_versions(pkg: PackageBase, args: Namespace) -> None:
    """output versions"""
    versions = _versions(pkg)
    safe = [v for v in versions if not pkg.versions[v].get("deprecated", False)]
    deprecated = [v for v in versions if pkg.versions[v].get("deprecated", False)]

    def url_for(version: spack.version.StandardVersion) -> str:
        return _url_for(pkg, version)

    def print_list(title: str, items: List[Any]) -> None:
        print()
        print(_title(f"{title}:"))
        if not items:
            print(" " * INDENT + "None")
        elif args.all:
            # one version per line with its download URL
            pad = max(len(str(v)) for v in items) + INDENT
            for v in items:
                print(f"{' ' * INDENT}{_ljust(_version_text(v), pad)}{url_for(v)}".rstrip())
        elif args.layout.tty:
            colify([_version_text(v) for v in items], indent=INDENT, tty=True)
        else:
            print(" " * INDENT + "  ".join(_version_text(v) for v in items))

    print_list("Safe versions", safe)
    if deprecated:
        print_list("Deprecated versions", deprecated)


# --- Small sections shown in the labeled block at the top ---------------------------------------


def print_maintainers(pkg: PackageBase, args: Namespace) -> None:
    """output package maintainers"""
    if pkg.maintainers:
        args.rows.append(("Maintainers", " ".join(f"@{m}" for m in pkg.maintainers)))


def print_preferred(pkg: PackageBase, args: Namespace) -> None:
    """output the version the spec resolves to according to the recipe"""
    if args.resolved_version is not None:
        args.rows.append(("Preferred", _version_text(args.resolved_version)))


def print_namespace(pkg: PackageBase, args: Namespace) -> None:
    """output package namespace and the path of the recipe"""
    repo = spack.repo.PATH.get_repo(pkg.namespace)
    args.rows.append(("Namespace", f"{color.colorize(f'@c{{{repo.namespace}}}')} at {repo.root}"))
    args.rows.append(("Recipe", repo.filename_for_package_name(pkg.name)))


def print_detectable(pkg: PackageBase, args: Namespace) -> None:
    """output information on external detection"""
    # A package with an 'executables' or 'libraries' attribute can detect installations. Without
    # determine_version/determine_variants it uses some custom detection mechanism.
    if hasattr(pkg, "executables") or hasattr(pkg, "libraries"):
        finds = [a for a in ("version", "variants") if hasattr(pkg, f"determine_{a}")]
        text = "True" + (f" ({', '.join(finds)})" if finds else "")
    else:
        text = "False"
    args.rows.append(("Externally detectable", text))


def print_tags(pkg: PackageBase, args: Namespace) -> None:
    """output package tags"""
    tags = sorted(getattr(pkg, "tags", ()))
    args.rows.append(("Tags", ", ".join(tags) if tags else "None"))


def print_phases(pkg: PackageBase, args: Namespace) -> None:
    """output installation phases"""
    builder = spack.builder.create(pkg)
    phases = getattr(builder, "phases", None)
    if phases:
        args.rows.append(("Phases", ", ".join(phases)))


def print_virtuals(pkg: PackageBase, args: Namespace) -> None:
    """output virtual packages"""
    lines = []
    for when, specs in reversed(sorted(pkg.provided.items())):
        provided = ", ".join(s.cformat() for s in sorted(specs))
        condition = "" if when == spack.spec.Spec(pkg.name) else f" when {when.cformat()}"
        lines.append(f"{provided}{condition}")
    args.rows.append(("Provides", "\n".join(lines) if lines else "None"))


def print_licenses(pkg: PackageBase, args: Namespace) -> None:
    """output the licenses of the project"""
    licenses = [
        f"{spdx} when {when.cformat()}" if when != spack.spec.Spec() else spdx
        for when, spdx in pkg.licenses.items()
        if pkg.intersects(when)
    ]
    if licenses:
        args.rows.append(("Licenses", "\n".join(licenses)))
    elif args.all:
        args.rows.append(("Licenses", "None"))


#: Show at most this many installed specs in the label block
MAX_INSTALLED_SHOWN = 8


def print_installed(pkg: PackageBase, args: Namespace) -> None:
    """output installations of the package matching the spec"""
    if args.virtual:
        installed = installed_providers(pkg.spec)
    else:
        installed = spack.store.STORE.db.query(pkg.spec)
    installed.sort(key=lambda s: (s.version, s.dag_hash()), reverse=True)
    installed.sort(key=lambda s: s.name)
    if not installed:
        args.rows.append(("Installed", "none"))
        return
    shown = [s.cformat("{name}{@version}{/hash:7}") for s in installed[:MAX_INSTALLED_SHOWN]]
    more = len(installed) - len(shown)
    text = ", ".join(shown) + (f" and {more} more" if more else "")
    args.rows.append(("Installed", f"{text}  (see: spack find -lv {pkg.name})"))


def print_configured(pkg: PackageBase, args: Namespace) -> None:
    """output externals and preferences configured for the package in packages.yaml"""
    configured = spack.config.CONFIG.get(f"packages:{pkg.name}", {}) or {}

    externals = []
    for external in configured.get("externals", []):
        spec = spack.spec.Spec(external["spec"])
        if not spec.intersects(pkg.spec):
            continue
        where = external.get("prefix") or ", ".join(external.get("modules", [])) or "?"
        via = "at" if external.get("prefix") else "via modules"
        externals.append(f"{spec.cformat()} {via} {where}")
    if externals:
        args.rows.append(("Externals", "\n".join(externals)))

    preferences = []
    for key in ("buildable", "require", "prefer", "conflict", "version", "variants"):
        if key in configured and configured[key] not in (True, None):
            value = configured[key]
            preferences.append(f"{key}: {value if isinstance(value, str) else json.dumps(value)}")
    if preferences:
        args.rows.append(("Preferences", "\n".join(preferences)))


def print_tests(pkg: PackageBase, args: Namespace) -> None:
    """output relevant build-time and stand-alone tests"""
    # Some built-in base packages (e.g., Autotools) define callback (e.g., check) inherited by
    # descendant packages. These checks may not result in build-time testing if the package's
    # build does not implement the expected functionality (e.g., a 'check' or 'test' target). So
    # the presence of a callback in Spack does not necessarily correspond to the actual presence
    # of build-time tests for a package.
    rows = []
    for attribute, phase in [
        ("build_time_test_callbacks", "Build phase tests"),
        ("install_time_test_callbacks", "Install phase tests"),
    ]:
        callbacks = getattr(pkg, attribute, None) or []
        names = sorted(name for name in callbacks if getattr(pkg, name, False))
        rows.append((phase, ", ".join(names) if names else "None"))

    # PackageBase defines an empty install/smoke test but we want to know if it has been
    # overridden and, therefore, assumed to be implemented.
    names = sorted(spack.install_test.test_function_names(pkg, add_virtuals=True))
    rows.append(("Stand-alone tests", ", ".join(names) if names else "None"))

    print()
    print_labeled(rows)


# --- Virtual packages ---------------------------------------------------------------------------


def provider_entries(spec: spack.spec.Spec) -> List[Entry]:
    """Who provides the virtual package ``spec``, and which version of it: one entry per provider
    constraint, e.g. ``mpich@:3.2`` provides ``mpi@:3.1``."""
    entries = []
    by_provided = spack.repo.PATH.provider_index.providers.get(spec.name, {})
    for provided, providers in by_provided.items():
        if not provided.intersects(spec):
            continue
        for provider in providers:
            plain = spack.spec.Spec(provider.format("{name}{@versions}{variants}"))  # no namespace
            entries.append(
                Entry(
                    _spec_text(plain),
                    head=plain.name,
                    attr=_spec_text(provided),
                    spec=plain,
                    sort_key=(plain.name, plain.versions, _natural(str(plain)), str(provided)),
                )
            )
    entries.sort(key=lambda e: e.sort_key)
    return entries


def installed_providers(spec: spack.spec.Spec) -> List[spack.spec.Spec]:
    """Installed specs that provide the virtual package ``spec``."""
    return [s for s in spack.store.STORE.db.query() if s.satisfies(spec)]


def print_providers(spec: spack.spec.Spec, args: Namespace) -> None:
    print_section("Providers", provider_entries(spec), args)


def _when_str(when: spack.spec.Spec) -> Optional[str]:
    return str(when) if when != spack.spec.Spec() else None


def info_json(pkg: PackageBase) -> Dict[str, Any]:
    """Everything `spack info` knows about the package, as plain data. Entries are filtered by
    the package's spec like the text output, but nothing is collapsed or merged."""
    resolved = resolved_version(pkg)
    virtual = spack.repo.PATH.is_virtual(pkg.name)
    phases = [] if virtual else list(getattr(spack.builder.create(pkg), "phases", None) or [])
    variants = []
    for name in spack.package_base._subkeys(pkg.variants):
        for when, variant in spack.package_base._definitions(pkg.variants, name):
            if not pkg.intersects(when):
                continue
            values: Optional[List[Dict[str, Any]]] = None
            if variant.values is not None:
                values = []
                for value in variant.values:
                    condition = None
                    if isinstance(value, spack.variant.ConditionalValue):
                        if value.when is None:
                            continue
                        value, condition = value.value, _when_str(value.when)
                    values.append({"value": _variant_value(value), "when": condition})
            variants.append(
                {
                    "name": name,
                    "default": _variant_value(variant.default),
                    "description": variant.description,
                    "when": _when_str(when),
                    "values": values,
                    "multi": variant.multi,
                    "sticky": variant.sticky,
                }
            )
    dependencies = [
        {
            "spec": str(dep.spec),
            "types": list(dt.flag_to_tuple(dep.depflag)),
            "when": _when_str(when),
            "virtual": spack.repo.PATH.is_virtual(name),
        }
        for name, deps in sorted(_dependencies_by_name(pkg).items())
        for when, dep in deps
    ]
    configured = spack.config.CONFIG.get(f"packages:{pkg.name}", {}) or {}
    installed = installed_providers(pkg.spec) if virtual else spack.store.STORE.db.query(pkg.spec)
    return {
        "name": pkg.name,
        "virtual": virtual,
        "providers": [
            {"provider": str(e.spec), "provides": color.csub(e.attr)}
            for e in provider_entries(pkg.spec)
        ],
        "namespace": pkg.namespace,
        "build_system_class": pkg.build_system_class,
        "description": " ".join((pkg.__doc__ or "").split()),
        "homepage": getattr(pkg, "homepage", None),
        "maintainers": list(pkg.maintainers),
        "tags": sorted(getattr(pkg, "tags", ())),
        "licenses": [
            {"license": spdx, "when": _when_str(when)}
            for when, spdx in pkg.licenses.items()
            if pkg.intersects(when)
        ],
        "phases": phases,
        "provides": [
            {
                "provides": sorted(str(s) for s in specs),
                "when": None if when == spack.spec.Spec(pkg.name) else _when_str(when),
            }
            for when, specs in sorted(pkg.provided.items())
        ],
        "resolved_version": str(resolved) if resolved is not None else None,
        "versions": [
            {
                "version": str(v),
                "preferred": v == resolved,
                "deprecated": bool(pkg.versions[v].get("deprecated", False)),
                "url": _url_for(pkg, v),
            }
            for v in _versions(pkg)
        ],
        "variants": variants,
        "dependencies": dependencies,
        "conflicts": [
            {"spec": str(spec), "when": _when_str(when), "message": _message(pkg, msg) or None}
            for when, conflicts in pkg.conflicts.items()
            if pkg.intersects(when)
            for spec, msg in conflicts
        ],
        "requirements": [
            {
                "specs": [str(s) for s in specs],
                "policy": policy,
                "when": _when_str(when),
                "message": _message(pkg, msg) or None,
            }
            for when, requirements in pkg.requirements.items()
            if pkg.intersects(when)
            for specs, policy, msg in requirements
        ],
        "patches": [
            {
                "patch": str(getattr(patch, "relative_path", None) or getattr(patch, "url", "")),
                "when": _when_str(when),
                "level": patch.level,
                "working_dir": patch.working_dir,
            }
            for when, patches in pkg.patches.items()
            if pkg.intersects(when)
            for patch in sorted(patches, key=lambda p: p.ordering_key)
        ],
        "installed": [
            {"spec": str(s), "hash": s.dag_hash(), "prefix": str(s.prefix)} for s in installed
        ],
        "externals": [
            e
            for e in configured.get("externals", [])
            if spack.spec.Spec(e["spec"]).intersects(pkg.spec)
        ],
        "preferences": {k: v for k, v in configured.items() if k != "externals"},
    }


class _VirtualPackage(PackageBase):
    # Stand-in for a virtual package that has no package.py of its own (no docstring on purpose:
    # `spack info` prints it as the description)

    has_code = False

    def __init__(self, spec: spack.spec.Spec) -> None:
        # PackageBase.__init__ looks the package up in the repository; a virtual is not there
        self.spec = spec

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.spec.name

    @property
    def namespace(self) -> Optional[str]:  # type: ignore[override]
        return None

    @property
    def homepage(self) -> Optional[str]:  # type: ignore[override]
        return None


def info(parser: argparse.ArgumentParser, args: Namespace) -> None:
    specs = spack.cmd.parse_specs(args.spec)
    if len(specs) > 1:
        args.subparser.error(f"requires exactly one spec, got {len(specs)}")
    if len(specs) == 0:
        args.subparser.error("requires a spec")

    spec = specs[0]
    args.virtual = spack.repo.PATH.is_virtual(spec.name)
    try:
        pkg_cls = spack.repo.PATH.get_pkg_class(spec.fullname)
    except spack.repo.UnknownPackageError:
        if not args.virtual:
            raise
        # a virtual without a package.py of its own: providers are all there is to show
        pkg_cls = _VirtualPackage
    pkg_cls.validate_variant_names(spec)
    pkg = pkg_cls(spec)

    if args.json:
        print(json.dumps(info_json(pkg), indent=2))
        return

    args.layout = Layout.detect()
    args.resolved_version = resolved_version(pkg)

    # name, build system and description
    kind = "virtual package" if args.virtual else pkg.build_system_class
    print(color.colorize(f"@*{{{color.cescape(pkg.name)}}} ({kind})"))
    doc = pkg.format_doc(indent=INDENT)
    print(doc.rstrip("\n") if doc else " " * INDENT + "No description")

    # short facts, as aligned "Label:  value" rows
    args.rows = []
    if getattr(pkg, "homepage", None):
        args.rows.append(("Homepage", str(pkg.homepage)))
    rows: List[Tuple[bool, Callable[[PackageBase, Namespace], None]]] = [
        (args.all or not args.no_versions, print_preferred),
        (True, print_licenses),
        (True, print_languages),
        (args.all or args.maintainers, print_maintainers),
        (args.all or args.namespace, print_namespace),
        (args.all or args.tags, print_tags),
        (args.all or args.detectable, print_detectable),
        (args.all or args.phases, print_phases),
        (args.all or args.virtuals, print_virtuals),
        (True, print_counts),
        (True, print_installed),
        (True, print_configured),
    ]
    for wanted, func in rows:
        if wanted:
            func(pkg, args)
    print()
    print_labeled(args.rows)

    # the long sections
    if args.virtual:
        print_providers(spec, args)
    if (args.all or not args.no_versions) and (pkg.versions or not args.virtual):
        print_versions(pkg, args)
    # a virtual's own package.py, if any, is a stub: its variants and dependencies mean nothing
    if (args.all or not args.no_variants) and not args.virtual:
        print_variants(pkg, args)
    if (args.all or not args.no_dependencies) and not args.virtual:
        print_section("Dependencies", dependency_entries(pkg), args)
    if args.all or args.conflicts:
        print_conflicts(pkg, args)
    if args.all or args.patches:
        print_patches(pkg, args)
    if args.all or args.tests:
        print_tests(pkg, args)
    print()
