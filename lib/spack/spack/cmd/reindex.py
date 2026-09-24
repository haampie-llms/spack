# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import os
import shutil

import spack.database
from spack.util import tty

description = "rebuild Spack's package database"
section = "admin"
level = "long"


def reindex(parser, args, ctx):
    current_index = ctx.store.db._index_path
    needs_backup = os.path.isfile(current_index)

    if needs_backup:
        backup = f"{current_index}.bkp"
        shutil.copy(current_index, backup)
        tty.msg("Created a backup copy of the DB at", backup)

    ctx.store.reindex()

    extra = ["If you need to restore, replace it with the backup."] if needs_backup else []
    tty.msg(
        f"The DB at {current_index} has been reindexed to v{spack.database._DB_VERSION}", *extra
    )
