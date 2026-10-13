# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

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


def validate_server_address(value: str) -> str:
    """Validate the address of a server the broker host will contact: a hostname or a routable IP.

    Loopback, link-local, unspecified, multicast and reserved addresses are refused, so a tenant
    cannot aim the login gate at services on the broker host or at the metadata address.

    Example:
        >>> validate_server_address("radius.example.test")
        'radius.example.test'
        >>> validate_server_address("10.0.0.5")
        '10.0.0.5'
        >>> validate_server_address("127.0.0.1")
        Traceback (most recent call last):
            ...
        ValueError: server address must be routable, not loopback, link-local or reserved
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return validate_hostname(value)
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    ):
        raise ValueError("server address must be routable, not loopback, link-local or reserved")
    return str(address)


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


def validate_persistence_cookie_name(value: str) -> str:
    """Validate a persistence cookie name (letters, digits, ``_`` and ``-``; 1-64 characters).

    Example:
        >>> validate_persistence_cookie_name("SY_SRV")
        'SY_SRV'
        >>> validate_persistence_cookie_name("a;b")
        Traceback (most recent call last):
            ...
        ValueError: cookie name must be 1-64 characters of letters, digits, '_' and '-'
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("cookie name must be 1-64 characters of letters, digits, '_' and '-'")
    return value


def validate_media_type(value: str) -> str:
    """Validate a media type such as ``text/html`` (no parameters, lower case).

    Example:
        >>> validate_media_type("image/svg+xml")
        'image/svg+xml'
        >>> validate_media_type("Text/HTML")
        Traceback (most recent call last):
            ...
        ValueError: media type must look like 'text/html' (lower case, no parameters)
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}", value):
        raise ValueError("media type must look like 'text/html' (lower case, no parameters)")
    return value


def validate_match_text(value: str) -> str:
    """Validate literal text a health check looks for (letters, digits and ``._:/-``; 1-128).

    Example:
        >>> validate_match_text("status:ok")
        'status:ok'
        >>> validate_match_text("a b")
        Traceback (most recent call last):
            ...
        ValueError: match text must be 1-128 characters of letters, digits and '._:/-'
    """
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", value):
        raise ValueError("match text must be 1-128 characters of letters, digits and '._:/-'")
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
#: Headers the authentication proxy uses between itself and the engine (ADR 0132).
AUTH_INTERNAL_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "x-forwarded-user",
        "x-forwarded-email",
        "x-forwarded-groups",
        "x-forwarded-preferred-username",
        "x-forwarded-access-token",
    }
)


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


_LABEL_TOKEN = re.compile(r"^[a-z0-9]([a-z0-9_.-]{0,61}[a-z0-9])?$")
#: Most labels one object may carry (ADR 0198).
MAX_LABELS = 20


def validate_labels(value: dict[str, str]) -> dict[str, str]:
    """Validate the labels of an object (ADR 0198).

    A key is 1 to 63 characters of ``a-z``, ``0-9``, ``-``, ``_`` and ``.``, starting and ending
    with a letter or digit. A value follows the same rule but may be empty. At most 20 labels.
    Labels are only for finding objects; they never reach a rendered configuration file.

    Example:
        >>> validate_labels({"env": "prod", "team": "payments"})
        {'env': 'prod', 'team': 'payments'}
        >>> validate_labels({"Env": "prod"})
        Traceback (most recent call last):
            ...
        ValueError: label key 'Env' must be 1-63 characters of a-z, 0-9, '-', '_', '.'
    """
    if len(value) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} labels")
    for key, val in value.items():
        if not _LABEL_TOKEN.fullmatch(key):
            raise ValueError(
                f"label key {key!r} must be 1-63 characters of a-z, 0-9, '-', '_', '.'"
            )
        if val and not _LABEL_TOKEN.fullmatch(val):
            raise ValueError(
                f"label value {val!r} must be empty or 1-63 characters of a-z, 0-9, '-', '_', '.'"
            )
    return value


def parse_label_selector(text: str) -> tuple[str, str, str | None]:
    """Parse one ``?label=`` selector into ``(key, operator, value)``.

    ``key`` matches objects that have the label, ``!key`` those that do not, ``key=value`` those
    where it equals the value and ``key!=value`` those where it is absent or different. The
    operator is one of ``has``, ``not``, ``eq`` and ``ne``.

    Example:
        >>> parse_label_selector("env=prod")
        ('env', 'eq', 'prod')
        >>> parse_label_selector("!legacy")
        ('legacy', 'not', None)
        >>> parse_label_selector("Env=prod")
        Traceback (most recent call last):
            ...
        ValueError: label key 'Env' must be 1-63 characters of a-z, 0-9, '-', '_', '.'
    """
    if "!=" in text:
        key, _, value = text.partition("!=")
        op = "ne"
    elif "=" in text:
        key, _, value = text.partition("=")
        op = "eq"
    elif text.startswith("!"):
        key, value, op = text[1:], None, "not"
    else:
        key, value, op = text, None, "has"
    validate_labels({key: value or ""})
    return key, op, value


def labels_match(labels: dict[str, str], selectors: list[str]) -> bool:
    """Whether ``labels`` satisfy every selector (see :func:`parse_label_selector`).

    Example:
        >>> labels_match({"env": "prod", "team": "pay"}, ["env=prod", "team"])
        True
        >>> labels_match({"env": "prod"}, ["env!=prod"])
        False
    """
    for text in selectors:
        key, op, value = parse_label_selector(text)
        present = key in labels
        ok = {
            "has": present,
            "not": not present,
            "eq": present and labels[key] == value,
            "ne": not present or labels[key] != value,
        }[op]
        if not ok:
            return False
    return True


_COUNTRY = re.compile(r"^[A-Z]{2}$")
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


_OIDC_ISSUER = re.compile(
    r"^https://([a-z0-9]([a-z0-9-]*[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?(/[A-Za-z0-9._~/%:@+-]{0,512})?$"
)
_CLAIM = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._~:@-]{1,128}$")
_GROUP = re.compile(r"^[A-Za-z0-9_.:/@=-]{1,128}$")


def validate_jwks_url(value: str) -> str:
    """Validate the address of a JSON Web Key Set: ``https``, a host, an optional port and path.

    No user info, query or fragment, at most 512 characters.

    Example:
        >>> validate_jwks_url("https://login.example.test/.well-known/jwks.json")
        'https://login.example.test/.well-known/jwks.json'
        >>> validate_jwks_url("http://login.example.test/jwks")
        Traceback (most recent call last):
            ...
        ValueError: jwks_url must be an https URL (host, optional port and path)
    """
    try:
        return validate_issuer(value)
    except ValueError:
        raise ValueError("jwks_url must be an https URL (host, optional port and path)") from None


def validate_issuer(value: str) -> str:
    """Validate an OpenID Connect issuer URL: ``https`` and a host, an optional port and path.

    No user info, query or fragment, and only characters that are safe in a quoted config value.

    Example:
        >>> validate_issuer("https://login.example.test/realms/acme")
        'https://login.example.test/realms/acme'
        >>> validate_issuer("http://login.example.test")
        Traceback (most recent call last):
            ...
        ValueError: issuer must be an https URL (host, optional port and path)
    """
    if not _OIDC_ISSUER.fullmatch(value) or len(value) > 512:
        raise ValueError("issuer must be an https URL (host, optional port and path)")
    host = value[len("https://") :].split("/", 1)[0].split(":", 1)[0]
    validate_hostname(host)
    return value


def validate_claim_name(value: str) -> str:
    """Validate a token claim name (for example ``groups``).

    Example:
        >>> validate_claim_name("groups")
        'groups'
    """
    if not _CLAIM.fullmatch(value):
        raise ValueError("claim name must be 1-64 characters of letters, digits and _ . : / -")
    return value


def validate_cookie_name(value: str) -> str:
    """Validate a session cookie name.

    Example:
        >>> validate_cookie_name("_sy_auth")
        '_sy_auth'
    """
    if not _COOKIE_NAME.fullmatch(value):
        raise ValueError("cookie name must be 1-32 characters of letters, digits, _ and -")
    return value


def validate_client_id(value: str) -> str:
    """Validate an OAuth client id.

    Example:
        >>> validate_client_id("switchyard-web")
        'switchyard-web'
    """
    if not _CLIENT_ID.fullmatch(value):
        raise ValueError("client_id must be 1-128 characters of letters, digits and . _ ~ : @ -")
    return value


def validate_group_name(value: str) -> str:
    """Validate an identity-provider group name or id.

    Example:
        >>> validate_group_name("/staff/admins")
        '/staff/admins'
    """
    if not _GROUP.fullmatch(value):
        raise ValueError("group must be 1-128 characters of letters, digits and . _ : / @ = -")
    return value


_PUBLIC_KEY = re.compile(
    r"^-----BEGIN PUBLIC KEY-----\n[A-Za-z0-9+/=\n]{40,4000}\n-----END PUBLIC KEY-----\n?$"
)
_CERT_NAME = re.compile(r"^[A-Za-z0-9._@:-]{1,128}$")


def validate_public_key_pem(value: str) -> str:
    r"""Validate a PEM public key block (never a private key).

    Example:
        >>> validate_public_key_pem("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----")
        Traceback (most recent call last):
            ...
        ValueError: public_key_pem must be one PEM PUBLIC KEY block
    """
    if not _PUBLIC_KEY.fullmatch(value):
        raise ValueError("public_key_pem must be one PEM PUBLIC KEY block")
    return value if value.endswith("\n") else value + "\n"


def validate_cert_name(value: str) -> str:
    """Validate a certificate common name or a user label used in a map file.

    Example:
        >>> validate_cert_name("alice@example.test")
        'alice@example.test'
        >>> validate_cert_name("a b")
        Traceback (most recent call last):
            ...
        ValueError: name must be 1-128 characters of letters, digits and . _ @ : -
    """
    if not _CERT_NAME.fullmatch(value):
        raise ValueError("name must be 1-128 characters of letters, digits and . _ @ : -")
    return value


_DN = re.compile(r"^[A-Za-z0-9=,._ -]{1,512}$")
_LDAP_FILTER = re.compile(r"^[A-Za-z0-9()=&|!*_.:@ -]{1,256}$")
_LDAP_ATTR = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}$")
_CERT_PEM = re.compile(
    r"^(-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]{40,6000}\n-----END CERTIFICATE-----\n?){1,5}$"
)


def validate_dn(value: str) -> str:
    """Validate an LDAP distinguished name (letters, digits and ``= , . _ -``, spaces).

    Example:
        >>> validate_dn("ou=People,dc=example,dc=test")
        'ou=People,dc=example,dc=test'
        >>> validate_dn("cn=a)(uid=*")
        Traceback (most recent call last):
            ...
        ValueError: distinguished name has characters that are not allowed
    """
    if not _DN.fullmatch(value):
        raise ValueError("distinguished name has characters that are not allowed")
    return value


def validate_ldap_filter(value: str) -> str:
    """Validate an LDAP search filter: balanced parentheses and plain characters.

    Example:
        >>> validate_ldap_filter("(&(objectClass=person)(memberOf=cn=staff))")
        '(&(objectClass=person)(memberOf=cn=staff))'
        >>> validate_ldap_filter("(uid=a")
        Traceback (most recent call last):
            ...
        ValueError: filter must have balanced parentheses
    """
    if not _LDAP_FILTER.fullmatch(value):
        raise ValueError("filter has characters that are not allowed")
    depth = 0
    for c in value:
        depth += (c == "(") - (c == ")")
        if depth < 0:
            break
    if depth != 0 or not value.startswith("("):
        raise ValueError("filter must have balanced parentheses")
    return value


def validate_ldap_attr(value: str) -> str:
    """Validate an LDAP attribute name.

    Example:
        >>> validate_ldap_attr("sAMAccountName")
        'sAMAccountName'
    """
    if not _LDAP_ATTR.fullmatch(value):
        raise ValueError("attribute must be a letter then up to 63 letters, digits and -")
    return value


def validate_email_address(value: str) -> str:
    """Validate a plain e-mail address.

    Example:
        >>> validate_email_address("alice@example.test")
        'alice@example.test'
    """
    if not _EMAIL.fullmatch(value) or ".." in value:
        raise ValueError("not an e-mail address")
    return value


def validate_cert_pem(value: str) -> str:
    """Validate one to five PEM CERTIFICATE blocks (public; never a key).

    Example:
        >>> validate_cert_pem("-----BEGIN PRIVATE KEY-----")
        Traceback (most recent call last):
            ...
        ValueError: expected PEM CERTIFICATE blocks only
    """
    if not _CERT_PEM.fullmatch(value):
        raise ValueError("expected PEM CERTIFICATE blocks only")
    return value if value.endswith("\n") else value + "\n"


def validate_country_code(value: str) -> str:
    """Validate an ISO 3166 alpha-2 country code (upper case, two letters).

    Example:
        >>> validate_country_code("NL")
        'NL'
        >>> validate_country_code("nld")
        Traceback (most recent call last):
            ...
        ValueError: country must be a two-letter upper-case ISO 3166 code
    """
    if not _COUNTRY.fullmatch(value):
        raise ValueError("country must be a two-letter upper-case ISO 3166 code")
    return value
