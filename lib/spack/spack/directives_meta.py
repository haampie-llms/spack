# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import collections
import functools
from typing import Any, Callable, Dict, Iterator, List, Set, Tuple, Type, TypeVar, Union

from spack.vendor.typing_extensions import ParamSpec

import spack.dependency
import spack.error
import spack.repo
import spack.spec

P = ParamSpec("P")
R = TypeVar("R")


def _merge_dependencies(merged: dict, own: dict) -> None:
    for when, own_deps in own.items():
        deps = merged.setdefault(when, {})
        for name, dep in own_deps.items():
            existing = deps.get(name)
            if existing is None:
                deps[name] = dep
            else:
                combined = spack.dependency.merge_dependencies(existing, dep)
                if combined.patches is None:
                    combined = spack.dependency.intern_dependency(combined)
                deps[name] = combined


def _merge_lists(merged: dict, own: dict) -> None:
    for when, items in own.items():
        merged.setdefault(when, []).extend(items)


def _merge_sets(merged: dict, own: dict) -> None:
    for when, items in own.items():
        merged.setdefault(when, set()).update(items)


#: The directive dictionaries the solver reads per class: each class in the MRO holds what its own
#: body declared (its "own" dictionary, populated once, see :func:`own_dict`), instead of every
#: subclass re-running the directives of all its base classes. The dictionary of a subclass is
#: the merge of those, built on access; the function for each dictionary is how a repeated entry
#: combines with an earlier one, and must do what re-running the directives would.
SHARDED_DICTS: Dict[str, Callable[[dict, dict], None]] = {
    "dependencies": _merge_dependencies,
    "extendees": dict.update,
    "conflicts": _merge_lists,
    "provided": _merge_sets,
    "provided_together": _merge_lists,
}

#: Names of possible directives. This list is mostly populated using the @directive decorator.
#: Some directives leverage others and in that case are not automatically added.
directive_names = ["build_system"]

SPEC_CACHE: Dict[str, spack.spec.Spec] = {}


def get_spec(spec_str: str) -> spack.spec.Spec:
    """Get a spec from the cache, or create it if not present."""
    if spec_str not in SPEC_CACHE:
        SPEC_CACHE[spec_str] = spack.spec._ImmutableSpec(spec_str)
    return SPEC_CACHE[spec_str]


class DirectiveMeta(type):
    """Flushes the directives that were temporarily stored in the staging
    area into the package.
    """

    #: Registry of {directive_name: [list_of_dicts_it_modifies]} populated by @directive
    _directive_to_dicts: Dict[str, Tuple[str, ...]] = {}
    #: Inverted index of {dict_name: [list_of_directives_modifying_it]}
    _dict_to_directives: Dict[str, List[str]] = collections.defaultdict(list)
    #: Maps dictionary name to its descriptor instance
    _descriptor_cache: Dict[str, "DirectiveDictDescriptor"] = {}
    #: Set of all known directive dictionary names from `@directive(dicts=...)`
    _directive_dict_names: Set[str] = set()
    #: Directives grouped by directive function name (e.g. "depends_on", "version", etc.). On
    #: the metaclass this is the staging queue of the class body being executed; on a package
    #: class it holds only the directives declared in that class's own body.
    _directives_to_be_executed: Dict[str, List[Callable]] = collections.defaultdict(list)
    #: Stack of when constraints from `with when(...)` context managers
    _when_constraints_stack: List[str] = []
    #: Stack of default args from `with default_args(...)` context managers
    _default_args_stack: List[dict] = []
    #: This property is set *automatically* during class definition as directives are invoked,
    #: if any ``depends_on`` or ``extends`` calls include patches for dependencies. This flag can
    #: be used as an optimization to detect whether a package provides patches for dependencies,
    #: without triggering the expensive deferred execution of those directives (without populating
    #: the ``dependencies`` dictionary).
    _patches_dependencies: bool = False

    def __new__(
        cls: Type["DirectiveMeta"], name: str, bases: tuple, attr_dict: dict
    ) -> "DirectiveMeta":
        attr_dict["_patches_dependencies"] = DirectiveMeta._patches_dependencies
        attr_dict["_directives_to_be_executed"] = dict(DirectiveMeta._directives_to_be_executed)
        DirectiveMeta._directives_to_be_executed.clear()
        DirectiveMeta._patches_dependencies = False

        # Add descriptors for all known directive dictionaries
        for dict_name in DirectiveMeta._directive_dict_names:
            # Where the actual data will be stored
            attr_dict[f"_{dict_name}"] = None
            # Descriptor to lazily initialize and populate the dictionary
            attr_dict[dict_name] = DirectiveMeta._get_descriptor(dict_name)
            if dict_name in SHARDED_DICTS:
                # Where the entries declared in this class's own body will be stored
                attr_dict[f"_own_{dict_name}"] = None

        return super(DirectiveMeta, cls).__new__(cls, name, bases, attr_dict)

    def __init__(cls: "DirectiveMeta", name: str, bases: tuple, attr_dict: dict):
        if spack.repo.is_package_module(cls.__module__):
            # Historically, maintainers was not a directive. They were simply set as class
            # attributes `maintainers = ["alice", "bob"]`. Therefore, we execute these directives
            # eagerly.
            for directive in DirectiveMeta._queued_directives(cls, "maintainers"):
                directive(cls)
        super(DirectiveMeta, cls).__init__(name, bases, attr_dict)

    @staticmethod
    def register_directive(name: str, dicts: Tuple[str, ...]) -> None:
        """Called by @directive to register relationships."""
        DirectiveMeta._directive_to_dicts[name] = dicts
        for d in dicts:
            DirectiveMeta._dict_to_directives[d].append(name)

    @staticmethod
    def directive_classes(cls: type) -> Iterator[type]:
        """The classes in the MRO of ``cls`` that declare directives, base classes first."""
        for klass in reversed(cls.__mro__):
            if isinstance(klass, DirectiveMeta) and klass.__dict__["_directives_to_be_executed"]:
                yield klass

    @staticmethod
    def _queued_directives(cls: type, name: str) -> Iterator[Callable]:
        """Directives of the given name queued for cls: base classes first, each class in the
        MRO once."""
        for klass in DirectiveMeta.directive_classes(cls):
            yield from klass.__dict__["_directives_to_be_executed"].get(name, ())

    @staticmethod
    def _get_descriptor(name: str) -> "DirectiveDictDescriptor":
        """Returns a singleton descriptor for the given dictionary name."""
        if name not in DirectiveMeta._descriptor_cache:
            DirectiveMeta._descriptor_cache[name] = DirectiveDictDescriptor(name)
        return DirectiveMeta._descriptor_cache[name]

    @staticmethod
    def push_when_constraint(when_spec: str) -> None:
        """Add a spec to the context constraints."""
        DirectiveMeta._when_constraints_stack.append(when_spec)

    @staticmethod
    def pop_when_constraint() -> str:
        """Pop the last constraint from the context"""
        return DirectiveMeta._when_constraints_stack.pop()

    @staticmethod
    def push_default_args(default_args: Dict[str, Any]) -> None:
        """Push default arguments"""
        DirectiveMeta._default_args_stack.append(default_args)

    @staticmethod
    def pop_default_args() -> dict:
        """Pop default arguments"""
        return DirectiveMeta._default_args_stack.pop()

    @staticmethod
    def _remove_kwarg_value_directives_from_queue(value) -> None:
        """Remove directives found in a kwarg value from the execution queue."""
        # Certain keyword argument values of directives may themselves be (lists of) directives. An
        # example of this is ``depends_on(..., patches=[patch(...), ...])``. In that case, we
        # should not execute those directives as part of the current package, but let the called
        # directive handle them. This function removes such directives from the execution queue.
        # directive instances are callable tuples, so exclude them from the descent
        if isinstance(value, (list, tuple)) and not callable(value):
            for item in value:
                DirectiveMeta._remove_kwarg_value_directives_from_queue(item)
        elif callable(value):  # directives are always callable
            # Remove directives args from the exec queue
            for lst in DirectiveMeta._directives_to_be_executed.values():
                for i, directive in enumerate(lst):
                    if value is directive:
                        del lst[i]
                        break

    @staticmethod
    def _get_execution_plan(target_dict: str) -> Tuple[List[str], List[str]]:
        """Calculates the closure of dicts and directives needed to populate target_dict."""
        dicts_involved = {target_dict}
        directives_involved = set()
        stack = [target_dict]

        while stack:
            current_dict = stack.pop()

            for directive_name in DirectiveMeta._dict_to_directives.get(current_dict, ()):
                if directive_name in directives_involved:
                    continue

                directives_involved.add(directive_name)

                for other_dict in DirectiveMeta._directive_to_dicts[directive_name]:
                    if other_dict not in dicts_involved:
                        dicts_involved.add(other_dict)
                        stack.append(other_dict)

        return sorted(dicts_involved), sorted(directives_involved)


class DirectiveDictDescriptor:
    """A descriptor that lazily executes directives on first access."""

    def __init__(self, name: str):
        self.name = name
        self.private_name = f"_{name}"
        self.own_name = f"_own_{name}"
        self.dicts_to_init, self.directives_to_run = DirectiveMeta._get_execution_plan(name)

    def __get__(self, obj, objtype=None):
        val = getattr(objtype, self.private_name)
        if val is not None:
            # populated; or, for a sharded dictionary, being populated by _populate_own()
            return val

        if self.name in SHARDED_DICTS:
            return self.merged(objtype)

        # The None value is a sentinel for "not yet initialized".
        for dictionary in self.dicts_to_init:
            if getattr(objtype, f"_{dictionary}") is None:
                setattr(objtype, f"_{dictionary}", {})

        # Populate these dictionaries by running all directives that modify them
        for directive_name in self.directives_to_run:
            for directive in DirectiveMeta._queued_directives(objtype, directive_name):
                directive(objtype)

        return getattr(objtype, self.private_name)

    def merged(self, objtype: type) -> dict:
        """The dictionary of a sharded group for ``objtype``: the own dictionaries of the classes
        in its MRO merged, base classes first, as running their directives in that order would.
        Built on every access and never stored, so that the own dictionaries are the only state;
        the solver reads those directly."""
        merged: dict = {}
        merge = SHARDED_DICTS[self.name]
        for klass in DirectiveMeta.directive_classes(objtype):
            merge(merged, self.own_dict(klass))
        return merged

    def own_dict(self, cls: type) -> dict:
        """The dictionary holding only what ``cls``'s own body declared. Populated on first use,
        together with the other dictionaries of its group, by running the directives queued for
        ``cls`` alone, so that a base class runs them once for all its subclasses."""
        own = cls.__dict__.get(self.own_name)
        return own if own is not None else self._populate_own(cls)[self.name]

    def own_dicts(self, cls: type) -> Dict[str, dict]:
        """The own dictionaries of the whole group, by name; see :meth:`own_dict`."""
        if cls.__dict__.get(self.own_name) is None:
            return self._populate_own(cls)
        return {d: cls.__dict__[f"_own_{d}"] for d in self.dicts_to_init}

    def _populate_own(self, cls: type) -> Dict[str, dict]:
        # Run the class's own directives with the group's dictionaries pointing at fresh ones, so
        # that directives writing to e.g. ``pkg.dependencies`` fill those in.
        own: Dict[str, dict] = {dictionary: {} for dictionary in self.dicts_to_init}
        for dictionary, value in own.items():
            setattr(cls, f"_{dictionary}", value)
        try:
            queued = cls.__dict__["_directives_to_be_executed"]
            for directive_name in self.directives_to_run:
                for directive in queued.get(directive_name, ()):
                    directive(cls)
        finally:
            for dictionary in own:
                setattr(cls, f"_{dictionary}", None)
        for dictionary, value in own.items():
            setattr(cls, f"_own_{dictionary}", value)
        return own


def own_dict(cls: type, name: str) -> dict:
    """Directive dictionary ``name`` holding only what ``cls``'s own body declared, see
    :meth:`DirectiveDictDescriptor.own_dict`."""
    return DirectiveMeta._get_descriptor(name).own_dict(cls)


def own_dicts(cls: type, name: str) -> Dict[str, dict]:
    """The own dictionaries of the group containing directive dictionary ``name`` for ``cls``,
    by name; see :meth:`DirectiveDictDescriptor.own_dict`."""
    return DirectiveMeta._get_descriptor(name).own_dicts(cls)


def own_items(cls: type, name: str) -> Iterator[Tuple[Any, Any]]:
    """The entries of directive dictionary ``name`` declared by ``cls`` and its base classes,
    base classes first, as ``(when, value)`` pairs. Unlike the merged dictionary this builds
    nothing, but the same ``when`` can come up once per class."""
    descriptor = DirectiveMeta._get_descriptor(name)
    for klass in DirectiveMeta.directive_classes(cls):
        yield from descriptor.own_dict(klass).items()


class directive:
    def __init__(
        self,
        dicts: Union[Tuple[str, ...], str] = (),
        supports_when: bool = True,
        can_patch_dependencies: bool = False,
    ) -> None:
        """Decorator for Spack directives.

        Spack directives allow you to modify a package while it is being defined, e.g. to add
        version or dependency information.  Directives are one of the key pieces of Spack's
        package "language", which is embedded in python.

        Here's an example directive::

            @directive(dicts="versions")
            def version(pkg, ...):
                ...

        This directive allows you write::

            class Foo(Package):
                version(...)

        The ``@directive`` decorator handles a couple things for you:

        1. Adds the class scope (pkg) as an initial parameter when called, like a class method
           would. This allows you to modify a package from within a directive, while the package is
           still being defined.

        2. It automatically adds a dictionary called ``versions`` to the package so that you can
           refer to pkg.versions.

        Arguments:
            dicts: A tuple of names of dictionaries to add to the package class if they don't
                already exist.
            supports_when: If True, the directive can be used within a ``with when(...)`` context
                manager. (To be removed when all directives support ``when=`` arguments.)
            can_patch_dependencies: If True, the directive can patch dependencies. This is used to
                identify nested directives so they can be removed from the execution queue, and to
                mark the package as patching dependencies.
        """

        if isinstance(dicts, str):
            dicts = (dicts,)

        # Add the dictionary names if not already there
        DirectiveMeta._directive_dict_names.update(dicts)

        self.supports_when = supports_when
        self.can_patch_dependencies = can_patch_dependencies
        self.dicts = tuple(dicts)

    def __call__(self, decorated_function: Callable[P, R]) -> Callable[P, R]:
        directive_names.append(decorated_function.__name__)
        DirectiveMeta.register_directive(decorated_function.__name__, self.dicts)

        @functools.wraps(decorated_function)
        def _wrapper(*args, **_kwargs):
            # First merge default args with kwargs
            if DirectiveMeta._default_args_stack:
                kwargs = {}
                for default_args in DirectiveMeta._default_args_stack:
                    kwargs.update(default_args)
                kwargs.update(_kwargs)
            else:
                kwargs = _kwargs

            # Inject when arguments from the `with when(...)` stack.
            if DirectiveMeta._when_constraints_stack:
                if not self.supports_when:
                    raise DirectiveError(
                        f'directive "{decorated_function.__name__}" cannot be used within a '
                        '"when" context since it does not support a "when=" argument'
                    )
                if "when" in kwargs:
                    kwargs["when"] = (*DirectiveMeta._when_constraints_stack, kwargs["when"])
                else:
                    kwargs["when"] = tuple(DirectiveMeta._when_constraints_stack)

            # Remove directives passed as arguments, so they are not executed as part of this
            # class's directive execution, but handled by the called directive instead
            if self.can_patch_dependencies and "patches" in kwargs:
                DirectiveMeta._remove_kwarg_value_directives_from_queue(kwargs["patches"])
                DirectiveMeta._patches_dependencies = True

            result = decorated_function(*args, **kwargs)

            DirectiveMeta._directives_to_be_executed[decorated_function.__name__].append(result)

            # wrapped function returns same result as original so that we can nest directives
            return result

        return _wrapper


class DirectiveError(spack.error.SpackError):
    """This is raised when something is wrong with a package directive."""
