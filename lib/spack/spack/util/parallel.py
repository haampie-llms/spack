# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import concurrent.futures
import multiprocessing
import os
import sys
import traceback
from typing import Any, Callable, Optional

from spack.util.cpus import cpus_available

#: Used in tests to disable parallelism, as tests themselves are parallelized
ENABLE_PARALLELISM = sys.platform != "win32"

#: Object shared by the tasks of a worker process, set once by the pool initializer
_SHARED: Any = None


def _init_worker(marshaler, shared: Any) -> None:
    global _SHARED
    marshaler.restore()
    _SHARED = shared


def _call_with_shared(fn: Callable, *args, **kwargs):
    return fn(_SHARED, *args, **kwargs)


class ErrorFromWorker:
    """Wrapper class to report an error from a worker process"""

    def __init__(self, exc_cls, exc, tb):
        """Create an error object from an exception raised from
        the worker process.

        The attributes of the process error objects are all strings
        as they are easy to send over a pipe.

        Args:
            exc: exception raised from the worker process
        """
        self.pid = os.getpid()
        self.error_message = str(exc)
        self.stacktrace_message = "".join(traceback.format_exception(exc_cls, exc, tb))

    @property
    def stacktrace(self):
        msg = "[PID={0.pid}] {0.stacktrace_message}"
        return msg.format(self)

    def __str__(self):
        return self.error_message


class Task:
    """Wrapped task that trap every Exception and return it as an
    ErrorFromWorker object.

    We are using a wrapper class instead of a decorator since the class
    is pickleable, while a decorator with an inner closure is not.
    """

    def __init__(self, func):
        self.func = func

    def __call__(self, *args, **kwargs):
        try:
            value = self.func(*args, **kwargs)
        except Exception:
            value = ErrorFromWorker(*sys.exc_info())
        return value


def imap_unordered(
    f,
    list_of_args,
    *,
    processes: int,
    maxtaskperchild: Optional[int] = None,
    debug=False,
    shared: Any = None,
):
    """Wrapper around multiprocessing.Pool.imap_unordered.

    Args:
        f: function to apply, called as ``f(shared, args)``
        list_of_args: list of tuples of args for the task
        shared: object sent once to each worker process, and passed to every task
        processes: maximum number of processes allowed
        debug: if False, raise an exception containing just the error messages
            from workers, if True an exception with complete stacktraces
        maxtaskperchild: number of tasks to be executed by a child before being
            killed and substituted

    Raises:
        RuntimeError: if any error occurred in the worker processes
    """

    if not ENABLE_PARALLELISM or len(list_of_args) <= 1:
        yield from (f(shared, args) for args in list_of_args)
        return

    from spack.subprocess_context import GlobalStateMarshaler

    marshaler = GlobalStateMarshaler()
    with multiprocessing.Pool(
        processes,
        initializer=_init_worker,
        initargs=(marshaler, shared),
        maxtasksperchild=maxtaskperchild,
    ) as p:
        for result in p.imap_unordered(Task(_SharedTask(f)), list_of_args):
            if isinstance(result, ErrorFromWorker):
                raise RuntimeError(result.stacktrace if debug else str(result))
            yield result


class _SharedTask:
    """Calls a function with the object shared by the worker process as first argument."""

    def __init__(self, func):
        self.func = func

    def __call__(self, args):
        return self.func(_SHARED, args)


class SequentialExecutor(concurrent.futures.Executor):
    """Executor that runs tasks sequentially in the current thread."""

    def __init__(self, shared: Any = None) -> None:
        self.shared = shared

    def submit_shared(self, fn, *args, **kwargs):
        """Submit a function that receives the shared object as first argument."""
        return self.submit(fn, self.shared, *args, **kwargs)

    def submit(self, fn, *args, **kwargs):
        """Submit a function to be executed."""
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as e:
            future.set_exception(e)
        return future


class _SharedProcessPoolExecutor(concurrent.futures.ProcessPoolExecutor):
    def submit_shared(self, fn, *args, **kwargs):
        """Submit a function that receives the shared object as first argument."""
        return self.submit(_call_with_shared, fn, *args, **kwargs)


def make_concurrent_executor(jobs: Optional[int] = None, *, shared: Any = None):
    """Create a concurrent executor.

    The ``shared`` object is sent once to each worker process, instead of with every task: tasks
    submitted with ``submit_shared`` receive it as first argument."""

    if not ENABLE_PARALLELISM or sys.version_info[:2] == (3, 6):
        return SequentialExecutor(shared)

    from spack.subprocess_context import GlobalStateMarshaler

    jobs = jobs or min(cpus_available(), 16)
    marshaler = GlobalStateMarshaler()
    return _SharedProcessPoolExecutor(  # novermin
        jobs, initializer=_init_worker, initargs=(marshaler, shared)
    )
