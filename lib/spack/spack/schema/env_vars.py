# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Schema for env_vars.yaml configuration file.

.. literalinclude:: _spack_root/lib/spack/spack/schema/env_vars.py
   :lines: 17-
"""

import copy
from typing import TYPE_CHECKING, Any, Dict

import spack.schema.environment

if TYPE_CHECKING:
    from spack.util.environment import EnvironmentModifications

#: Environment modifications, plus filters on the names of the modified variables
env_vars: Dict[str, Any] = copy.deepcopy(spack.schema.environment.env_modifications)
env_vars["properties"]["include"] = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Only modify these environment variables: modifications of other variables, "
    "including those of the environment view, are dropped",
}
env_vars["properties"]["exclude"] = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Do not modify these environment variables: their modifications, including "
    "those of the environment view, are dropped",
}

properties: Dict[str, Any] = {"env_vars": env_vars}

#: Full schema with metadata
schema = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Spack env_vars configuration file schema",
    "type": "object",
    "additionalProperties": False,
    "properties": properties,
}


def parse(config_obj: Dict[str, Any]) -> "EnvironmentModifications":
    """Returns the environment modifications in an ``env_vars`` configuration."""
    return spack.schema.environment.parse(
        {k: v for k, v in config_obj.items() if k not in ("include", "exclude")}
    )


def filter_modifications(env: "EnvironmentModifications", config_obj: Dict[str, Any]) -> None:
    """Drops the modifications of variables not allowed by the ``include`` and ``exclude`` lists
    of an ``env_vars`` configuration."""
    include = config_obj.get("include")
    exclude = config_obj.get("exclude", [])
    env.env_modifications = [
        m
        for m in env.env_modifications
        if (include is None or m.name in include) and m.name not in exclude
    ]
