# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from typing import Callable

from ._functions import _host, by_name, platforms, reset
from ._platform import Platform
from .darwin import Darwin
from .freebsd import FreeBSD
from .linux import Linux
from .test import Test
from .windows import Windows

__all__ = [
    "Platform",
    "Darwin",
    "Linux",
    "FreeBSD",
    "Test",
    "Windows",
    "platforms",
    "host",
    "by_name",
    "reset",
    "using_libc_compatibility",
]

#: The "real" platform of the host running Spack. This should not be changed
#: by any method and is here as a convenient way to refer to the host platform.
real_host = _host

#: The current platform used by Spack. Tests replace it with a mock platform.
host: Callable[[], Platform] = _host


class _PickleableCallable:
    """Class used to pickle a callable that may substitute either
    _platform or _all_platforms. Lambda or nested functions are
    not pickleable.
    """

    def __init__(self, return_value):
        self.return_value = return_value

    def __call__(self):
        return self.return_value


def using_libc_compatibility() -> bool:
    """Returns True if we are using libc compatibility on this platform."""
    return host().name == "linux"
