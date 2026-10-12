"""Zone helpers shared by the DNS providers."""


def zone_candidates(name: str) -> list[str]:
    """Return the parent domains of ``name`` to try as zones, longest first (at least 2 labels).

    Example:
        >>> zone_candidates("_acme-challenge.a.example.co.uk")
        ['a.example.co.uk', 'example.co.uk', 'co.uk']
    """
    labels = name.strip(".").split(".")
    return [".".join(labels[i:]) for i in range(1, len(labels) - 1)]


def relative_name(name: str, zone: str) -> str:
    """The record name inside ``zone`` (``@`` for the apex).

    Example:
        >>> relative_name("_acme-challenge.example.test", "example.test")
        '_acme-challenge'
        >>> relative_name("example.test", "example.test")
        '@'
    """
    name, zone = name.strip(".").lower(), zone.strip(".").lower()
    if name == zone:
        return "@"
    if not name.endswith("." + zone):
        raise ValueError(f"{name} is not inside the zone {zone}")
    return name[: -len(zone) - 1]


def in_zone(name: str, zone: str) -> bool:
    """True when ``name`` is the zone or inside it.

    Example:
        >>> in_zone("a.example.test", "example.test"), in_zone("example.test.evil", "example.test")
        (True, False)
    """
    name, zone = name.strip(".").lower(), zone.strip(".").lower()
    return name == zone or name.endswith("." + zone)
