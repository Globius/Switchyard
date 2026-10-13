# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Proxmox reports a missing object as HTTP 500 with a message, not 404 (seen on a real PVE 9)."""

import httpx

from switchyard.adapters.proxmox import _is_missing


def test_real_proxmox_missing_messages_are_recognised() -> None:
    for message in (
        "Configuration file 'nodes/pve/qemu-server/9900.conf' does not exist\n",
        "sdn zone 'sytest' does not exist\n",
        "no such VM ('101')",
    ):
        assert _is_missing(httpx.Response(500, json={"data": None, "message": message}))


def test_other_errors_are_not_missing() -> None:
    assert _is_missing(httpx.Response(404))
    assert not _is_missing(httpx.Response(500, json={"message": "VM is locked (create)"}))
    assert not _is_missing(httpx.Response(403, json={"message": "does not exist"}))
