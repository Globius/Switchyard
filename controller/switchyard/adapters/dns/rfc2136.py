"""RFC 2136 dynamic updates signed with a TSIG key (BIND, Knot, PowerDNS and others).

An update that adds a value leaves the other values of the name alone, and one that removes a value
removes only that value, so wildcard and apex challenges can share ``_acme-challenge.<zone>``.
"""

import asyncio

import dns.exception
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.tsigkeyring
import dns.update

from switchyard.adapters.dns.base import DnsError
from switchyard.adapters.dns.zones import in_zone


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Rfc2136:
    """Dynamic DNS updates for ACME DNS-01."""

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
            response = dns.query.tcp(update, self._server, port=self._port, timeout=self._timeout)
        except (dns.exception.DNSException, OSError) as exc:
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

    async def close(self) -> None:
        """Nothing to release."""
