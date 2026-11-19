..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. meta::
   :description lang=en:
      Learn how to modify shell environment variables within a Spack environment using the env_vars.yaml file.

.. index::
   single: env_vars.yaml
   :name: env-vars-yaml

Environment Variable Settings (env_vars.yaml)
=============================================

Spack allows you to include shell environment variable modifications for a Spack environment by including an ``env_vars.yaml`` file.
Environment variables can be modified by setting, unsetting, appending, and prepending variables in the shell environment.
The changes to the shell environment will take effect when the Spack environment is activated.

For example:

.. code-block:: yaml

  env_vars:
    set:
      ENVAR_TO_SET_IN_ENV_LOAD: "FOO"
    unset:
    - ENVAR_TO_UNSET_IN_ENV_LOAD
    prepend_path:
      PATH_LIST: "path/to/prepend"
    append_path:
      PATH_LIST: "path/to/append"
    remove_path:
      PATH_LIST: "path/to/remove"

These modifications are applied after those of the environment's view, so they take precedence.

``include`` and ``exclude`` restrict which environment variables are modified at all, by the view as well as by ``env_vars`` itself.
For example, to only modify ``PATH``, and not variables like ``CMAKE_PREFIX_PATH`` or ``PKG_CONFIG_PATH`` that the view would set:

.. code-block:: yaml

  env_vars:
    include: [PATH]

Variables listed in ``exclude`` are not modified.
Both filters also apply to the environment variables of an image created with ``spack buildcache push --tag <tag> --env-view``.
