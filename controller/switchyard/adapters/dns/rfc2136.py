# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""RFC 2136 dynamic updates signed with a TSIG key (BIND, Knot, PowerDNS and others).

An update that adds a value leaves the other values of the name alone, and one that removes a value
removes only that value, so wildcard and apex challenges can share ``_acme-challenge.<zone>``.

Address records for a virtual service (ADR 0193) work the same way: the handle of a record the
controller created is its value, and before writing anything the name is read back from the server
so a record someone else made is never overwritten.
"""

import asyncio

import dns.exception
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype
import dns.tsigkeyring
import dns.update

from switchyard.adapters.dns.base import DnsError, address_target, resolve_endpoint
from switchyard.adapters.dns.zones import in_zone

_NO_PROXY = "RFC 2136 has no proxy: set proxied to false for records on this credential"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Rfc2136:
    """Dynamic DNS updates for ACME DNS-01 and for virtual services (ADR 0193)."""

    def __init__(
        self,
        server: str,
        zone: str,
        key_name: str,
        tsig_secret: str,
        *,
        port: int = 53,
        algorithm: str = "hmac-sha256",
        timeout_s: float = 10.0,
    ) -> None:
        """Create the client; ``tsig_secret`` is the base64 key as in the key file."""
        self._server = server.strip("[]")
        self._port = port
        self._zone = zone.strip(".")
        self._keyname = key_name
        self._keyring = dns.tsigkeyring.from_text({key_name: tsig_secret})
        self._algorithm = algorithm
        self._timeout = timeout_s

    def _send(self, update: dns.update.UpdateMessage) -> None:
        try:
            address = resolve_endpoint(self._server, "server")[0]
            response = dns.query.tcp(update, address, port=self._port, timeout=self._timeout)
        except (dns.exception.DNSException, OSError, ValueError) as exc:
            raise DnsError(f"RFC 2136 update to {self._server}: {exc}") from exc
        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            raise DnsError(f"RFC 2136 update refused: {dns.rcode.to_text(rcode)}")

    def _update(self) -> dns.update.UpdateMessage:
        return dns.update.Update(
            self._zone,
            keyring=self._keyring,
            keyname=dns.name.from_text(self._keyname),
            keyalgorithm=self._algorithm,
        )

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Add ``value`` to the TXT set of ``name``."""
        if not in_zone(name, self._zone):
            raise DnsError(f"{name} is not inside the zone {self._zone}")
        update = self._update()
        update.add(f"{name}.", ttl, "TXT", _quote(value))
        await asyncio.to_thread(self._send, update)
        return value

    async def delete_txt(self, name: str, handle: str) -> None:
        """Remove the one value ``handle`` from the TXT set of ``name``."""
        update = self._update()
        update.delete(f"{name}.", "TXT", _quote(handle))
        await asyncio.to_thread(self._send, update)

    def _query(self, name: str, rtype: str) -> tuple[int, list[str]]:
        """Read one record set from the server: its TTL and values (empty when there is none)."""
        message = dns.message.make_query(dns.name.from_text(name), rtype)
        message.use_tsig(
            self._keyring, dns.name.from_text(self._keyname), algorithm=self._algorithm
        )
        try:
            address = resolve_endpoint(self._server, "server")[0]
            response = dns.query.tcp(message, address, port=self._port, timeout=self._timeout)
        except (dns.exception.DNSException, OSError, ValueError) as exc:
            raise DnsError(f"RFC 2136 query to {self._server}: {exc}") from exc
        rcode = response.rcode()
        if rcode not in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
            raise DnsError(f"RFC 2136 query refused: {dns.rcode.to_text(rcode)}")
        wanted = dns.rdatatype.from_text(rtype)
        for rrset in response.answer:
            if rrset.rdtype == wanted:
                return int(rrset.ttl), [str(r) for r in rrset]
        return 0, []

    async def upsert_address(
        self, name: str, address: str, *, ttl: int, proxied: bool, handle: str | None
    ) -> str:
        """Create or update an ``A``/``AAAA`` record (ADR 0193); the handle is its value.

        The name is read back first: a record that this controller did not create is adopted only
        when its value already equals ``address`` and is otherwise refused (:class:`DnsError`).
        """
        clean, rtype, value = address_target(name, address, self._zone)
        if proxied:
            raise DnsError(_NO_PROXY)
        current_ttl, current = await asyncio.to_thread(self._query, clean, rtype)
        known = handle.strip() if handle is not None else None
        update = self._update()
        if known is not None and known in current:
            if known == value and current_ttl == ttl:
                return value
            update.delete(f"{clean}.", rtype, known)  # ours: replace it (and its TTL)
        else:
            foreign = [v for v in current if v != value]
            if foreign:
                raise DnsError(
                    f"{clean} already has an {rtype} record ({', '.join(foreign)}) that this "
                    "controller did not create"
                )
            if value in current and current_ttl == ttl:
                return value
            if value in current:
                update.delete(f"{clean}.", rtype, value)  # same value, new TTL
        update.add(f"{clean}.", ttl, rtype, value)
        await asyncio.to_thread(self._send, update)
        return value

    async def delete_address(self, name: str, handle: str) -> None:
        """Remove the record ``handle`` (its value) from ``name`` (idempotent)."""
        clean, rtype, value = address_target(name, handle, self._zone)
        _, current = await asyncio.to_thread(self._query, clean, rtype)
        if value not in current:
            return
        update = self._update()
        update.delete(f"{clean}.", rtype, value)
        await asyncio.to_thread(self._send, update)

    async def close(self) -> None:
        """Nothing to release."""
