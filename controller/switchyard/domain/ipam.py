"""VIP address allocation (IPAM). Pure functions, no I/O.

A VIP pool lists inclusive ranges on one network. Allocation is deterministic: the lowest usable
address that is not in use. Usable means a host address of the network that is not the gateway
(for IPv4 prefixes shorter than /31 the network and broadcast addresses are excluded).
"""

from collections.abc import Iterable, Iterator

from switchyard.domain.models import IPAddress, Network, VipPool


class PoolExhaustedError(Exception):
    """The pool has no free address."""


def _usable(network: Network, address: IPAddress) -> bool:
    net = network.cidr
    if address not in net:
        return False
    if address == network.gateway:
        return False
    if net.version == 4 and net.prefixlen < 31:
        return address not in (net.network_address, net.broadcast_address)
    return True


def candidates(pool: VipPool, network: Network) -> Iterator[IPAddress]:
    """Yield the pool's usable addresses in ascending order, without duplicates.

    Example:
        >>> import uuid, ipaddress
        >>> from switchyard.domain.models import IpRange
        >>> net = Network(id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="n",
        ...               cidr="192.0.2.0/29", gateway="192.0.2.1")
        >>> pool = VipPool(id=uuid.uuid4(), tenant_id=net.tenant_id, name="p", network_id=net.id,
        ...                ranges=(IpRange(start="192.0.2.0", end="192.0.2.4"),))
        >>> [str(a) for a in candidates(pool, net)]
        ['192.0.2.2', '192.0.2.3', '192.0.2.4']
    """
    seen: set[IPAddress] = set()
    for r in sorted(pool.ranges, key=lambda r: int(r.start)):
        current = int(r.start)
        end = int(r.end)
        cls = type(r.start)
        while current <= end:
            address = cls(current)
            current += 1
            if address in seen or not _usable(network, address):
                continue
            seen.add(address)
            yield address


def allocate(pool: VipPool, network: Network, used: Iterable[IPAddress]) -> IPAddress:
    """Return the lowest free usable address of ``pool``.

    Raises:
        PoolExhaustedError: If every usable address is in ``used``.
    """
    taken = set(used)
    for address in candidates(pool, network):
        if address not in taken:
            return address
    raise PoolExhaustedError(f"VIP pool {pool.name} has no free address")
