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
import json
import re
import shutil
import sys
from argparse import Namespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, TextIO, Tuple

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
from spack.util import tty
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
#: Suggest disabling a boolean variant when at least this many dependencies are conditional on it
SUGGESTION_THRESHOLD = 20


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

    options = [
        ("--detectable", print_detectable.__doc__),
        ("--maintainers", print_maintainers.__doc__),
        ("--namespace", print_namespace.__doc__),
        ("--no-dependencies", f"do not {print_dependencies.__doc__}"),
        ("--no-variants", f"do not {print_variants.__doc__}"),
        ("--no-versions", f"do not {print_versions.__doc__}"),
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
    """

    __slots__ = ("name", "head", "attr", "description", "when", "when_text", "extra", "sort_key")

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
    ) -> None:
        self.name = name
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

    def fields(self, show_when: bool) -> List[str]:
        """The text fields after the name and attribute columns, in display order."""
        result = []
        if self.description:
            result.append(self.description)
        if show_when and self.when_text:
            result.append(color.colorize(f"{WHEN_COLOR}{{when}} ") + self.when_text)
        result.extend(self.extra)
        return result


def _spec_text(spec: Optional[spack.spec.Spec]) -> str:
    """Colorized (if enabled) string for a spec, empty for no spec or an empty spec."""
    if spec is None or spec == spack.spec.Spec():
        return ""
    # an anonymous spec with only key=value variants renders with a leading blank inside the
    # color codes, e.g. "\x1b[0;94m build_system=cmake\x1b[0m"
    return re.sub(r"^((?:\x1b\[[0-9;]*m)*)\s+", r"\1", spec.clong_spec)


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
    ) -> None:
        entries = list(entries)
        self.layout = layout
        self.indent = indent
        self.out = out or sys.stdout
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

            if self.layout.tty:
                self._print_tty(name, entry.attr, entry.fields(show_when))
            else:
                self._print_line(name, entry.attr, entry.fields(show_when))

    def _print_line(self, name: str, attr: str, fields: List[str]) -> None:
        """One line per entry. Only a description with explicit line breaks (detailed help for a
        variant, say) continues on further lines, indented; the first line still carries the
        condition and the values."""
        columns = [" " * self.indent + _ljust(name, self.name_width)]
        if self.attr_width:
            columns.append(_ljust(attr, self.attr_width))
        continuation: List[str] = []
        for i, field in enumerate(fields):
            lines = [" ".join(line.split()) for line in field.split("\n")]
            columns.append(lines[0])
            if i == 0:
                continuation = lines[1:]
            else:
                columns[-1] = " ".join(lines)
        text_column = self.indent + self.name_width + GUTTER
        if self.attr_width:
            text_column += self.attr_width + GUTTER
        self.out.write((" " * GUTTER).join(columns).rstrip() + "\n")
        for line in continuation:
            self.out.write(" " * text_column + line + "\n")

    def _print_tty(self, name: str, attr: str, fields: List[str]) -> None:
        """Name and attribute in their columns, then the fields one below the other, wrapped."""
        text_column = self.indent + self.name_width + GUTTER
        if self.attr_width:
            text_column += self.attr_width + GUTTER

        prefix = " " * self.indent + _ljust(name, self.name_width) + " " * GUTTER
        if color.clen(name) > self.name_width:
            # overlong name: continue on the next line at the attribute column
            self.out.write(prefix.rstrip() + "\n")
            prefix = " " * (self.indent + self.name_width + GUTTER)
        if self.attr_width:
            prefix += _ljust(attr, self.attr_width) + " " * GUTTER

        if not fields:
            self.out.write(prefix.rstrip() + "\n")
            return

        indent = " " * text_column
        for i, field in enumerate(fields):
            first_indent = prefix if i == 0 else indent
            for line in _wrap(field, first_indent, indent, self.layout.width):
                self.out.write(line.rstrip() + "\n")


def print_section(title: str, entries: List[Entry], layout: Layout, by_name: bool) -> None:
    """Print a titled section of entries, either in name order with their conditions inline, or
    grouped by condition (unconditional entries first)."""
    print()
    print(_title(f"{title}:"))
    if not entries:
        print(" " * INDENT + "None")
        return

    entries = merge_conditions(entries)
    printer = EntryPrinter(entries, layout)
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


def _replace_component(text: str, needle: str, replacement: str) -> str:
    """Replace one rendered component of a spec (``cuda_arch=10``, ``@1.8``, ``%c``, ...) in
    possibly colorized spec text, where it is preceded by a blank, a color code or the start of
    the text and not followed by more of the same component (so that ``cuda_arch=10`` does not
    match inside ``cuda_arch=100``)."""
    pattern = rf"(^|\s|\x1b\[[0-9;]*m){re.escape(needle)}(?![{_COMPONENT_CHARS}])"
    return re.sub(pattern, lambda m: m.group(1) + replacement, text)


def _condition_parts(when: spack.spec.Spec) -> Dict[str, str]:
    """Split a condition into its components, keyed by what they constrain, each rendered as it
    appears in the plain string of the spec."""
    parts: Dict[str, str] = {}
    if when.versions != spack.version.any_version:
        parts["version"] = when.format("{@versions}")
    for name in sorted(when.variants):
        parts[f"variant:{name}"] = str(when.variants[name])
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
    cut = max(prefix.rfind(c) for c in "=@%^")
    prefix = prefix[: cut + 1] if cut >= 0 else ""
    return prefix + "{" + ",".join(v[len(prefix) :] for v in values) + "}"


class _MergedCondition:
    """Entries whose conditions differ in exactly one component, being merged into one."""

    __slots__ = ("entry", "parts", "varying", "values")

    def __init__(self, entry: Entry, parts: Dict[str, str]) -> None:
        self.entry = entry
        self.parts = parts
        self.varying: Optional[str] = None
        self.values = [parts]

    def absorb(self, parts: Dict[str, str]) -> bool:
        if parts.keys() != self.parts.keys():
            return False
        differing = [k for k in parts if parts[k] != self.parts[k]]
        if len(differing) != 1 or (self.varying is not None and differing[0] != self.varying):
            return False
        key = differing[0]
        if key.startswith("variant:") and parts[key][0] in "+~":  # keep +x and ~x apart
            return False
        self.varying = key
        self.values.append(parts)
        return True

    def merged_entry(self) -> Entry:
        entry = self.entry
        if self.varying is None:
            return entry
        needle = self.parts[self.varying]
        alternatives = _braces([parts[self.varying] for parts in self.values])
        entry.when_text = _replace_component(entry.when_text, needle, alternatives)
        return entry


def merge_conditions(entries: List[Entry]) -> List[Entry]:
    """Merge entries that say the same thing under conditions that differ in one component only,
    e.g. ``cuda@12.9: when cuda_arch=103`` and ``cuda@12.9: when cuda_arch=103a`` into
    ``cuda@12.9: when cuda_arch={103,103a}``. Order is that of the first entry of each merge."""
    merges: Dict[Tuple, List[_MergedCondition]] = {}
    result: List[Entry] = []
    merged_entries: List[Tuple[Entry, _MergedCondition]] = []
    for entry in entries:
        if entry.when is None or PLACEHOLDER in entry.when_key:
            result.append(entry)
            continue
        key = (
            color.csub(entry.name),
            color.csub(entry.attr),
            entry.description,
            tuple(entry.extra),
        )
        parts = _condition_parts(entry.when)
        for merge in merges.get(key, []):
            if merge.absorb(parts):
                break
        else:
            merge = _MergedCondition(entry, parts)
            merges.setdefault(key, []).append(merge)
            result.append(entry)
            merged_entries.append((entry, merge))
    for entry, merge in merged_entries:
        merge.merged_entry()
    return result


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


def dependency_entries(pkg: PackageBase) -> List[Entry]:
    """Entries for the dependencies of ``pkg`` that are possible for its spec.

    Runs of dependencies that only forward a variant value, e.g. ``kokkos cuda_arch=X`` when
    ``cuda_arch=X`` for every ``X``, are collapsed into a single entry with a placeholder.
    """
    # unlike variants, dependency declarations never override each other: every one applies
    # whenever its condition holds, so all of them are shown
    by_name: Dict[str, List[Tuple[spack.spec.Spec, spack.dependency.Dependency]]] = {}
    for when, deps in pkg.dependencies.items():
        if not pkg.intersects(when):
            continue
        for name, dep in deps.items():
            by_name.setdefault(name, []).append((when, dep))

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
                            _spec_text(dep.spec),
                            head=name,
                            attr=_deptypes(dep.depflag),
                            when=when,
                            sort_key=sort_key(when, dep) + (_natural(when.long_spec),),
                        )
                    )
                continue

            dep_text, when_text = _spec_text(dep.spec), _spec_text(when)
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
    print_section("Dependencies", dependency_entries(pkg), args.layout, args.by_name)


def print_dependency_suggestion(pkg: PackageBase, entries: List[Entry]) -> None:
    """Suggest disabling boolean variants that many dependencies are conditional on."""
    counts: Dict[Tuple[str, bool], int] = collections.defaultdict(int)
    for entry in entries:
        if entry.when is None:
            continue
        for variant in entry.when.variants.values():
            if variant.type == spack.variant.VariantType.BOOL:
                counts[(variant.name, variant.value)] += 1

    spec = spack.spec.Spec(pkg.name)
    for (name, value), count in sorted(counts.items(), key=lambda kv: -kv[1]):
        # skip variants the user already set, and variants that appear with both values
        if count < SUGGESTION_THRESHOLD or name in pkg.spec.variants or name in spec.variants:
            continue
        spec.variants.set(spack.variant.BoolValuedVariant(name, not value))

    if spec.variants:
        spec.constrain(pkg.spec)  # include already specified constraints
        print()
        tty.info(
            f"{pkg.name} has many conditional dependencies; for a simpler view, try:",
            f"spack info {spec.format(color=color.get_color_when())}",
            format="y",
        )


# --- Variants -----------------------------------------------------------------------------------


def _variant_value(v: Any) -> str:
    return str(v).lower() if v is None or isinstance(v, bool) else str(v)


def _variant_values(variant: spack.variant.Variant) -> str:
    """The allowed values of a non-boolean variant, empty for boolean variants and for variants
    whose values are checked by a validator function."""
    values = variant.possible_values()
    if values is None or len(values) < 2 or all(isinstance(v, bool) for v in values):
        return ""
    rendered = ", ".join(color.cescape(_variant_value(v)) for v in values)
    kind = "any of" if variant.multi else "one of"
    return color.colorize(f"{kind}: @c{{{rendered}}}")


def variant_entries(pkg: PackageBase) -> List[Entry]:
    entries = []
    for name in spack.package_base._subkeys(pkg.variants):
        for when, variant in spack.package_base._definitions(pkg.variants, name):
            if not pkg.intersects(when):
                continue
            default = color.cescape(_variant_value(variant.default))
            values = _variant_values(variant)
            entries.append(
                Entry(
                    color.colorize(f"@c{{{color.cescape(name)}}} @C{{[{default}]}}"),
                    description=variant.description,
                    when=when,
                    extra=[values] if values else [],
                )
            )
    return entries


def print_variants(pkg: PackageBase, args: Namespace) -> None:
    """output variants"""
    print_section("Variants", variant_entries(pkg), args.layout, args.by_name)


# --- Versions -----------------------------------------------------------------------------------


def _version_text(v: Any) -> str:
    return color.colorize(f"{spack.spec.VERSION_COLOR}{{{color.cescape(str(v))}}}")


def print_versions(pkg: PackageBase, args: Namespace) -> None:
    """output versions"""
    versions = sorted((v for v in pkg.versions if pkg.spec.versions.intersects(v)), reverse=True)
    preferred = spack.package_base.preferred_version(pkg) if versions else None
    safe = [v for v in versions if not pkg.versions[v].get("deprecated", False)]
    deprecated = [v for v in versions if pkg.versions[v].get("deprecated", False)]

    def url_for(version: spack.version.VersionType) -> str:
        if not pkg.has_code:
            return ""
        try:
            return str(spack.package_base.for_package_version(pkg, version))
        except fs.InvalidArgsError:
            return "No URL"

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

    print_list("Preferred version", [preferred] if preferred is not None else [])
    print_list("Safe versions", safe)
    if deprecated:
        print_list("Deprecated versions", deprecated)


# --- Small sections shown in the labeled block at the top ---------------------------------------


def print_maintainers(pkg: PackageBase, args: Namespace) -> None:
    """output package maintainers"""
    if pkg.maintainers:
        args.rows.append(("Maintainers", " ".join(f"@{m}" for m in pkg.maintainers)))


def print_namespace(pkg: PackageBase, args: Namespace) -> None:
    """output package namespace"""
    repo = spack.repo.PATH.get_repo(pkg.namespace)
    args.rows.append(("Namespace", f"{color.colorize(f'@c{{{repo.namespace}}}')} at {repo.root}"))


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
    installed = sorted(
        spack.store.STORE.db.query(pkg.spec), key=lambda s: (s.version, s.dag_hash()), reverse=True
    )
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


def info(parser: argparse.ArgumentParser, args: Namespace) -> None:
    specs = spack.cmd.parse_specs(args.spec)
    if len(specs) > 1:
        args.subparser.error(f"requires exactly one spec, got {len(specs)}")
    if len(specs) == 0:
        args.subparser.error("requires a spec")

    spec = specs[0]
    pkg_cls = spack.repo.PATH.get_pkg_class(spec.fullname)
    pkg_cls.validate_variant_names(spec)
    pkg = pkg_cls(spec)

    args.layout = Layout.detect()

    # name, build system and description
    print(color.colorize(f"@*{{{color.cescape(pkg.name)}}} ({pkg.build_system_class})"))
    doc = pkg.format_doc(indent=INDENT)
    print(doc.rstrip("\n") if doc else " " * INDENT + "No description")

    # short facts, as aligned "Label:  value" rows
    args.rows = []
    if getattr(pkg, "homepage", None):
        args.rows.append(("Homepage", str(pkg.homepage)))
    rows: List[Tuple[bool, Callable[[PackageBase, Namespace], None]]] = [
        (True, print_licenses),
        (args.all or args.maintainers, print_maintainers),
        (args.all or args.namespace, print_namespace),
        (args.all or args.tags, print_tags),
        (args.all or args.detectable, print_detectable),
        (args.all or args.phases, print_phases),
        (args.all or args.virtuals, print_virtuals),
        (True, print_installed),
        (True, print_configured),
    ]
    for wanted, func in rows:
        if wanted:
            func(pkg, args)
    print()
    print_labeled(args.rows)

    # the long sections
    if args.all or not args.no_versions:
        print_versions(pkg, args)
    if args.all or not args.no_variants:
        print_variants(pkg, args)
    dependencies: List[Entry] = []
    if args.all or not args.no_dependencies:
        dependencies = dependency_entries(pkg)
        print_section("Dependencies", dependencies, args.layout, args.by_name)
    if args.all or args.tests:
        print_tests(pkg, args)

    print_dependency_suggestion(pkg, dependencies)
    print()
