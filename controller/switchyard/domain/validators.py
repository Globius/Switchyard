"""Strict validators for every user-supplied value that can reach a config file.

These are the first of two guards (the renderer escapes again). Each ``validate_*`` function either
returns the normalised value or raises ``ValueError`` with a message safe to show to a user.
"""

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?$")
# Conservative: nothing HAProxy treats specially (whitespace, quotes, backslash, '#', '$', braces,
# brackets, parentheses). '%' is allowed only for percent-encoding.
_PATH = re.compile(r"^/[A-Za-z0-9._~/%:@+,;=-]{0,1023}$")
_LOCATION_TAIL = re.compile(r"^[A-Za-z0-9._~/%:@+,;=?&-]{0,1023}$")
_HEADER = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")
# Header values are rendered inside double quotes: no quote, backslash, ``$`` (environment
# expansion), ``%`` (log-format variables), ``#`` or control characters. Spaces are allowed inside.
_VALUE_CHARS = r"A-Za-z0-9._~/:@+,;=*()&!'-"
_HEADER_VALUE = re.compile(rf"^[{_VALUE_CHARS}]([ {_VALUE_CHARS}]{{0,254}}[{_VALUE_CHARS}])?$")
_BAD_VALUE = "header value must be 1-256 characters without quotes, backslash, $, % or #"
_QUERY_PARAM = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SECRET_REF = re.compile(r"^[a-z0-9][a-z0-9/_.-]{0,254}$")


def validate_name(value: str) -> str:
    """Validate an object name (unique per tenant, used in config section and file names).

    Lowercase letters, digits, ``-`` and ``_``; starts and ends alphanumeric; at most 63 characters.
    No dots: names become file names and the Data Plane API rewrites dots in those.

    Example:
        >>> validate_name("web-prod")
        'web-prod'
        >>> validate_name("Web Prod")
        Traceback (most recent call last):
            ...
        ValueError: name must be 1-63 characters: a-z, 0-9, '-' or '_'
    """
    if not _NAME.fullmatch(value) and not re.fullmatch(r"[a-z0-9]", value):
        raise ValueError("name must be 1-63 characters: a-z, 0-9, '-' or '_'")
    return value


def validate_hostname(value: str) -> str:
    """Validate and lower-case an RFC 1123 hostname.

    No trailing dot, no IP literals, ASCII only (use punycode for IDNs), labels of 1-63 characters,
    at most 253 characters in total.

    Example:
        >>> validate_hostname("Files.Example.COM")
        'files.example.com'
        >>> validate_hostname("*.example.com")
        Traceback (most recent call last):
            ...
        ValueError: invalid hostname label '*'
    """
    host = value.lower()
    if not host or len(host) > 253:
        raise ValueError("hostname must be 1-253 characters")
    if host.endswith("."):
        raise ValueError("hostname must not end with a dot")
    for label in host.split("."):
        if not _LABEL.fullmatch(label):
            raise ValueError(f"invalid hostname label {label!r}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("hostname must not be an IP address")


def validate_host_pattern(value: str) -> str:
    """Validate a hostname or a single-label wildcard such as ``*.example.com``.

    Example:
        >>> validate_host_pattern("*.Example.com")
        '*.example.com'
        >>> validate_host_pattern("a.*.example.com")
        Traceback (most recent call last):
            ...
        ValueError: invalid hostname label '*'
    """
    if value.startswith("*."):
        return "*." + validate_hostname(value[2:])
    return validate_hostname(value)


def is_wildcard(pattern: str) -> bool:
    """Return True for ``*.domain`` patterns."""
    return pattern.startswith("*.")


def host_covered_by_san(host: str, san: str) -> bool:
    """Return True if certificate SAN ``san`` covers route host ``host``.

    Follows RFC 6125: a wildcard matches exactly one label and never the apex. A wildcard route
    host (``*.example.com``) is only covered by the identical wildcard SAN.

    Example:
        >>> host_covered_by_san("a.example.com", "*.example.com")
        True
        >>> host_covered_by_san("example.com", "*.example.com")
        False
        >>> host_covered_by_san("a.b.example.com", "*.example.com")
        False
        >>> host_covered_by_san("*.example.com", "*.example.com")
        True
        >>> host_covered_by_san("*.a.example.com", "*.example.com")
        False
    """
    host, san = host.lower(), san.lower()
    if host == san:
        return True
    if is_wildcard(san) and not is_wildcard(host):
        suffix = san[1:]  # ".example.com"
        return host.endswith(suffix) and "." not in host[: -len(suffix)] and len(host) > len(suffix)
    return False


def uncovered_hosts(hosts: Iterable[str], sans: Iterable[str]) -> list[str]:
    """Return the hosts that no SAN covers, sorted and de-duplicated.

    Example:
        >>> uncovered_hosts(["a.example.com", "b.other.org"], ["*.example.com"])
        ['b.other.org']
    """
    san_list = list(sans)
    return sorted({h for h in hosts if not any(host_covered_by_san(h, s) for s in san_list)})


def validate_port(value: int) -> int:
    """Validate a TCP/UDP port number (1-65535).

    Example:
        >>> validate_port(443)
        443
        >>> validate_port(0)
        Traceback (most recent call last):
            ...
        ValueError: port must be between 1 and 65535
    """
    if isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return value


def validate_path(value: str) -> str:
    """Validate a URL path (prefix or exact) used in route matches.

    Starts with ``/`` and uses only characters HAProxy never treats specially.

    Example:
        >>> validate_path("/.well-known/carddav")
        '/.well-known/carddav'
        >>> validate_path("/a b")
        Traceback (most recent call last):
            ...
        ValueError: path must start with '/' and use only A-Z a-z 0-9 and . _ ~ / % : @ + , ; = -
    """
    if not _PATH.fullmatch(value):
        raise ValueError(
            "path must start with '/' and use only A-Z a-z 0-9 and . _ ~ / % : @ + , ; = -"
        )
    return value


def validate_location(value: str) -> str:
    """Validate a redirect target: an absolute path or an ``http(s)://host[:port]/path`` URL.

    Example:
        >>> validate_location("/remote.php/dav/")
        '/remote.php/dav/'
        >>> validate_location("https://Example.com:8443/x?y=1")
        'https://example.com:8443/x?y=1'
        >>> validate_location("javascript:alert(1)")
        Traceback (most recent call last):
            ...
        ValueError: location must be an absolute path or an http(s) URL
    """
    if value.startswith("/"):
        if not _LOCATION_TAIL.fullmatch(value[1:]) or value.startswith("//"):
            raise ValueError("location path contains forbidden characters")
        return value
    m = re.fullmatch(r"^(https?)://([^/:?]+)(:\d{1,5})?(/.*)?$", value)
    if not m:
        raise ValueError("location must be an absolute path or an http(s) URL")
    scheme, host, port, rest = m.group(1), m.group(2), m.group(3), m.group(4) or "/"
    host = validate_hostname(host)
    if port:
        validate_port(int(port[1:]))
    if not _LOCATION_TAIL.fullmatch(rest[1:]):
        raise ValueError("location path contains forbidden characters")
    return f"{scheme}://{host}{port or ''}{rest}"


def validate_header_name(value: str) -> str:
    """Validate an HTTP header name (letters, digits and ``-``).

    Example:
        >>> validate_header_name("CF-Connecting-IP")
        'CF-Connecting-IP'
        >>> validate_header_name("X Bad")
        Traceback (most recent call last):
            ...
        ValueError: header name must be 1-64 characters of letters, digits and '-'
    """
    if not _HEADER.fullmatch(value):
        raise ValueError("header name must be 1-64 characters of letters, digits and '-'")
    return value


def validate_interface(value: str) -> str:
    """Validate a network interface name (Linux ``IFNAMSIZ``: at most 15 characters).

    Example:
        >>> validate_interface("eth1")
        'eth1'
        >>> validate_interface("eth 1")
        Traceback (most recent call last):
            ...
        ValueError: interface must be 1-15 characters of a-z, 0-9, '.', '_' or '-' (letter first)
    """
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,14}", value):
        raise ValueError(
            "interface must be 1-15 characters of a-z, 0-9, '.', '_' or '-' (letter first)"
        )
    return value


def validate_secret_ref(value: str) -> str:
    """Validate an OpenBao path reference (never a secret itself).

    Example:
        >>> validate_secret_ref("kv/tenants/acme/certs/wildcard")
        'kv/tenants/acme/certs/wildcard'
        >>> validate_secret_ref("../etc/passwd")
        Traceback (most recent call last):
            ...
        ValueError: secret_ref must be a lower-case OpenBao path without '..' segments
    """
    if not _SECRET_REF.fullmatch(value) or ".." in value or "//" in value:
        raise ValueError("secret_ref must be a lower-case OpenBao path without '..' segments")
    return value


#: Secret namespaces that belong to the platform; no tenant object may point into them.
RESERVED_SECRET_PREFIXES = ("kv/proxmox/", "kv/platform/", "kv/system/", "kv/controller/")


@dataclass
class SecretPolicy:
    """Which secret paths tenant objects may name besides their own ``kv/tenants/<tenant>/``.

    Attributes:
        shared_prefixes: Paths every tenant may use, for example ``("kv/se/",)`` for a Data Plane
            API credential shared by all engines. Each must end with ``/``.
        open: Allow any path outside the tenant and platform namespaces (labs and tests).
    """

    shared_prefixes: tuple[str, ...] = ()
    open: bool = False


_POLICY = SecretPolicy()


def configure_secret_policy(shared_prefixes: tuple[str, ...] = (), *, open: bool = False) -> None:  # noqa: A002
    """Set the secret path policy for this process (called once at start-up).

    Raises:
        ValueError: If a shared prefix is not a path ending in ``/`` or is a reserved one.
    """
    for prefix in shared_prefixes:
        if not prefix.endswith("/") or validate_secret_ref(prefix.rstrip("/")) != prefix.rstrip(
            "/"
        ):
            raise ValueError(f"shared secret prefix {prefix!r} must be a path ending in '/'")
        if prefix.startswith(RESERVED_SECRET_PREFIXES) or "kv/tenants/".startswith(prefix):
            raise ValueError(f"shared secret prefix {prefix!r} overlaps a reserved namespace")
    _POLICY.shared_prefixes = tuple(shared_prefixes)
    _POLICY.open = open


def check_secret_ref_scope(ref: str, tenant_name: str) -> str | None:
    """Say why a tenant may not use ``ref``, or return ``None`` if it may.

    A tenant's objects may point at its own namespace (``kv/tenants/<tenant>/...``) or at one of
    the configured shared prefixes (``SecretPolicy``); never at another tenant's secrets or at the
    platform's own (the controller would read them and hand them to the tenant's engines). With
    ``open`` any other path is allowed too (labs).

    Example:
        >>> check_secret_ref_scope("kv/tenants/acme/certs/x/01", "acme") is None
        True
        >>> check_secret_ref_scope("kv/tenants/globex/certs/x/01", "acme")
        'kv/tenants/globex/certs/x/01 belongs to another tenant'
        >>> check_secret_ref_scope("kv/proxmox/token", "acme")
        'kv/proxmox/token is a platform secret'
    """
    if ref.startswith(RESERVED_SECRET_PREFIXES):
        return f"{ref} is a platform secret"
    parts = ref.split("/")
    if ref.startswith("kv/tenants/"):
        if len(parts) < 3 or parts[2] != tenant_name:
            return f"{ref} belongs to another tenant"
        return None
    if _POLICY.open or ref.startswith(_POLICY.shared_prefixes):
        return None
    return f"{ref} is outside the tenant's namespace and the shared prefixes"


def validate_vip_in_network(vip: IPAddress, network: IPNetwork) -> None:
    """Raise ``ValueError`` unless ``vip`` is a usable host address inside ``network``.

    The network and (for IPv4 prefixes of /30 or shorter) broadcast addresses are not usable.

    Example:
        >>> import ipaddress as ip
        >>> validate_vip_in_network(ip.ip_address("192.0.2.20"), ip.ip_network("192.0.2.0/24"))
        >>> validate_vip_in_network(ip.ip_address("192.0.2.255"), ip.ip_network("192.0.2.0/24"))
        Traceback (most recent call last):
            ...
        ValueError: 192.0.2.255 is the broadcast address of 192.0.2.0/24
    """
    if vip.version != network.version:
        raise ValueError(f"{vip} is IPv{vip.version} but {network} is IPv{network.version}")
    if vip not in network:
        raise ValueError(f"{vip} is outside {network}")
    if network.prefixlen < network.max_prefixlen - 1:
        if vip == network.network_address:
            raise ValueError(f"{vip} is the network address of {network}")
        if isinstance(vip, ipaddress.IPv4Address) and vip == network.broadcast_address:
            raise ValueError(f"{vip} is the broadcast address of {network}")


@dataclass(frozen=True)
class Listener:
    """One ``(owner, vip, port, transport)`` binding, for collision checks.

    Attributes:
        owner: Name of the virtual service that owns the binding.
        vip: Bind address.
        port: Bind port.
        transport: ``"tcp"`` for HTTP and TCP services, ``"udp"`` for UDP services.
    """

    owner: str
    vip: IPAddress
    port: int
    transport: str = "tcp"


def find_port_collisions(listeners: Iterable[Listener]) -> list[tuple[Listener, Listener]]:
    """Return every pair of listeners that bind the same VIP, port and transport.

    Example:
        >>> import ipaddress as ip
        >>> a = Listener("a", ip.ip_address("192.0.2.1"), 443)
        >>> b = Listener("b", ip.ip_address("192.0.2.1"), 443)
        >>> c = Listener("c", ip.ip_address("192.0.2.1"), 80)
        >>> find_port_collisions([a, b, c]) == [(a, b)]
        True
    """
    seen: dict[tuple[IPAddress, int, str], Listener] = {}
    out: list[tuple[Listener, Listener]] = []
    for item in sorted(listeners, key=lambda x: (x.vip.version, int(x.vip), x.port, x.owner)):
        key = (item.vip, item.port, item.transport)
        if key in seen:
            out.append((seen[key], item))
        else:
            seen[key] = item
    return out


def host_port(host: str, port: int) -> str:
    """``host:port`` with an IPv6 literal in brackets, as URLs and socket targets need it.

    Example:
        >>> host_port("10.0.0.5", 5556)
        '10.0.0.5:5556'
        >>> host_port("2001:db8::5", 5556)
        '[2001:db8::5]:5556'
    """
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"


_DISCOVERY_TAG = re.compile(r"^[a-z0-9_][a-z0-9_+.-]{0,63}$")


def validate_discovery_tag(value: str) -> str:
    """Validate a Proxmox tag used for pool discovery.

    Proxmox tags are lower-case and may contain ``a-z``, ``0-9``, ``_``, ``+``, ``.`` and ``-``.
    Which tags a tenant may use is a cross-object rule (:func:`discovery_tag_prefix`).

    Example:
        >>> validate_discovery_tag("sy-acme.web")
        'sy-acme.web'
        >>> validate_discovery_tag("Web Tier")
        Traceback (most recent call last):
            ...
        ValueError: tag must be 1-64 characters: a-z, 0-9, '_', '+', '.' or '-'
    """
    if not _DISCOVERY_TAG.fullmatch(value):
        raise ValueError("tag must be 1-64 characters: a-z, 0-9, '_', '+', '.' or '-'")
    return value


def discovery_tag_prefix(tenant_name: str) -> str:
    """The prefix every discovery tag of a tenant must start with.

    Proxmox tags are cluster-wide, so a tenant may only discover guests whose tag begins with
    ``sy-<tenant>.``. Tenant names never contain a dot, so ``sy-acme.`` cannot be a prefix of the
    tags of a tenant called ``acme-x`` (ADR 0145).

    Example:
        >>> discovery_tag_prefix("acme")
        'sy-acme.'
    """
    return f"sy-{tenant_name}."


def validate_header_value(value: str) -> str:
    """Validate a literal header value (match operand or value to set).

    One to 256 characters of ``A-Z a-z 0-9`` and ``. _ ~ / : @ + , ; = * ( ) & ! ' -``, with
    single spaces allowed between them. No quotes, backslash, ``$``, ``%`` or ``#``: a value that
    must vary per request uses a variable from the fixed list instead.

    Example:
        >>> validate_header_value("max-age=31536000; includeSubDomains")
        'max-age=31536000; includeSubDomains'
        >>> validate_header_value("%[src]")
        Traceback (most recent call last):
            ...
        ValueError: header value must be 1-256 characters without quotes, backslash, $, % or #
    """
    if not _HEADER_VALUE.fullmatch(value):
        raise ValueError(_BAD_VALUE)
    return value


def validate_query_param(value: str) -> str:
    """Validate a query-string parameter name.

    Example:
        >>> validate_query_param("utm_source")
        'utm_source'
        >>> validate_query_param("a b")
        Traceback (most recent call last):
            ...
        ValueError: query parameter name must be 1-64 characters of letters, digits, '_', '.' or '-'
    """
    if not _QUERY_PARAM.fullmatch(value):
        raise ValueError(
            "query parameter name must be 1-64 characters of letters, digits, '_', '.' or '-'"
        )
    return value


#: Headers a policy may never set, add or remove: message framing and hop-by-hop headers.
FORBIDDEN_HEADERS = frozenset(
    {
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "expect",
        "proxy-authorization",
        "proxy-authenticate",
        "proxy-connection",
    }
)
#: Request headers the renderer owns (client-address hygiene); a policy cannot touch them.
MANAGED_REQUEST_HEADERS = frozenset({"x-forwarded-for", "x-forwarded-proto"})


def validate_body(value: str) -> str:
    r"""Validate the text of a fixed response or an error page.

    At most 16 KiB of UTF-8, no NUL or other control characters except tab, CR and LF. The text is
    stored as a file and served as is, so it needs no escaping, but it must not break the file
    format or the HTTP message.

    Example:
        >>> validate_body("Down for maintenance")
        'Down for maintenance'
        >>> validate_body("a\x00b")
        Traceback (most recent call last):
            ...
        ValueError: body must not contain control characters
    """
    if len(value.encode("utf-8")) > 16_384:
        raise ValueError("body must be at most 16 KiB")
    if any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in value):
        raise ValueError("body must not contain control characters")
    return value
