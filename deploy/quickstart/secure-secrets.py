# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Restrict quickstart credentials to their operator and the one service that needs them."""

import argparse
import os
from pathlib import Path


def secure(directory: Path, owner_uid: int, owner_gid: int) -> None:
    """Protect existing credentials without reading, replacing or printing their contents.

    The controller reads its keys as group 10001; the TLS front reads only server.key as
    group 101. Administrative tokens and the unseal key remain readable only by the operator.
    """
    administrative = (
        "openbao-init.json",
        "unseal.key",
        "root.token",
        "dp_password",
        "admin-token",
        "breakglass.txt",
        "bg.json",
        "bg.code",
        "server.json",
        "client.json",
    )
    files = [(name, owner_gid, 0o600) for name in administrative]
    files += [
        ("client.key", 10001, 0o640),
        ("bundle.key", 10001, 0o640),
        ("server.key", 101, 0o640),
    ]
    files += [(path.name, owner_gid, 0o644) for path in directory.glob("*.crt")]
    files += [("ca.pem", owner_gid, 0o644)]
    for name, group, mode in files:
        path = directory / name
        if path.is_symlink():
            raise ValueError(f"refusing a symlink at {name}")
        if path.exists():
            os.chmod(path, 0o600)  # remove broad access before changing ownership
            os.chown(path, owner_uid, group)
            os.chmod(path, mode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("owner_uid", type=int)
    parser.add_argument("owner_gid", type=int)
    args = parser.parse_args()
    secure(args.directory, args.owner_uid, args.owner_gid)
