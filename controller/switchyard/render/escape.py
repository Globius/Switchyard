r"""Escaping and strict token checks for values that reach ``haproxy.cfg``.

Domain validators already restrict every user value to a safe character set. These functions are the
second guard (backend design, "Input safety"): the renderer never trusts its input. A value that
does not fit is rejected with :class:`RenderError` instead of being "fixed", because a silently
altered config is worse than a failed render.

HAProxy parses configuration words with these hazards, all of which the checks rule out:

- whitespace and ``#`` end or split a word, quotes and ``\\`` start quoting/escaping,
- ``$`` and ``${...}`` expand environment variables,
- ``%`` starts a log-format variable in ``redirect`` targets (``%%`` is a literal percent),
- newlines end the line.
"""

import ipaddress
import re


class RenderError(ValueError):
    """Raised when a value cannot be rendered safely or the snapshot cannot be rendered."""


_IDENT = re.compile(r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?")
_ARG = re.compile(r"[A-Za-z0-9._~/%:@+,;=*-]+")
_LOCATION = re.compile(r"[A-Za-z0-9._~/%:@+,;=?&*-]+")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
_MATCH = re.compile(r"[A-Za-z0-9._:/-]{1,128}")
_COOKIE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEADER = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?")
_VALUE = re.compile(r"[A-Za-z0-9 ._~/:@+,;=*()&!'-]+")
_PATH = re.compile(r"/[A-Za-z0-9._~/%:@+,;=-]*")
_PARAM = re.compile(r"[A-Za-z0-9_.-]+")


def ident(value: str) -> str:
    """Return ``value`` if it is a safe section/file name, else raise.

    Example:
        >>> ident("web-prod_1")
        'web-prod_1'
        >>> ident("a b")
        Traceback (most recent call last):
            ...
        switchyard.render.escape.RenderError: unsafe identifier 'a b'
    """
    if not _IDENT.fullmatch(value):
        raise RenderError(f"unsafe identifier {value!r}")
    return value


def arg(value: str) -> str:
    """Return ``value`` if it is safe as an unquoted configuration word, else raise.

    Example:
        >>> arg("/.well-known/carddav")
        '/.well-known/carddav'
        >>> arg("/a;b c")
        Traceback (most recent call last):
            ...
        switchyard.render.escape.RenderError: unsafe value '/a;b c'
    """
    if not _ARG.fullmatch(value):
        raise RenderError(f"unsafe value {value!r}")
    return value


def location(value: str) -> str:
    """Return ``value`` ready for a ``redirect location/prefix`` argument (log-format context).

    ``%`` is doubled so percent-encoded text stays literal.

    Example:
        >>> location("/remote.php/dav/?a=1&b=2")
        '/remote.php/dav/?a=1&b=2'
        >>> location("/a%20b")
        '/a%%20b'
    """
    if not _LOCATION.fullmatch(value):
        raise RenderError(f"unsafe redirect location {value!r}")
    return value.replace("%", "%%")


def header_name(value: str) -> str:
    """Return ``value`` if it is a plain HTTP header name, else raise.

    Example:
        >>> header_name("CF-Connecting-IP")
        'CF-Connecting-IP'
    """
    if not _HEADER.fullmatch(value):
        raise RenderError(f"unsafe header name {value!r}")
    return value


def quoted(value: str) -> str:
    """Return ``value`` in double quotes if it is a safe literal header value, else raise.

    Example:
        >>> quoted("max-age=60; includeSubDomains")
        '"max-age=60; includeSubDomains"'
        >>> quoted('a"b')
        Traceback (most recent call last):
            ...
        switchyard.render.escape.RenderError: unsafe header value 'a"b'
    """
    if not _VALUE.fullmatch(value):
        raise RenderError(f"unsafe header value {value!r}")
    return f'"{value}"'


def query_param(value: str) -> str:
    """Return ``value`` if it is a plain query-parameter name, else raise.

    Example:
        >>> query_param("utm_source")
        'utm_source'
    """
    if not _PARAM.fullmatch(value):
        raise RenderError(f"unsafe query parameter {value!r}")
    return value


def path_text(value: str) -> str:
    """Return ``value`` if it is a safe literal path for a ``replace-path`` replacement.

    ``%`` is doubled because the replacement is a log-format string.

    Example:
        >>> path_text("/v2/a%20b")
        '/v2/a%%20b'
    """
    if not _PATH.fullmatch(value):
        raise RenderError(f"unsafe path {value!r}")
    return value.replace("%", "%%")


def path_regex(prefix: str) -> str:
    """Return the regex source for a literal path prefix (``.`` and ``+`` written as classes).

    Example:
        >>> path_regex("/api/v1.2")
        '/api/v1[.]2'
    """
    if not _PATH.fullmatch(prefix):
        raise RenderError(f"unsafe path {prefix!r}")
    return prefix.replace(".", "[.]").replace("+", "[+]")


def cookie_name(value: str) -> str:
    """Return ``value`` if it is a plain cookie name, else raise.

    Example:
        >>> cookie_name("SY_SRV")
        'SY_SRV'
        >>> cookie_name("a=b")
        Traceback (most recent call last):
            ...
        switchyard.render.escape.RenderError: unsafe cookie name 'a=b'
    """
    if not _COOKIE.fullmatch(value):
        raise RenderError(f"unsafe cookie name {value!r}")
    return value


def match_text(value: str) -> str:
    """Return ``value`` if it is safe literal text for a health check match, else raise.

    Example:
        >>> match_text("status_ok")
        'status_ok'
        >>> match_text('a"b')
        Traceback (most recent call last):
            ...
        switchyard.render.escape.RenderError: unsafe match text 'a"b'
    """
    if not _MATCH.fullmatch(value):
        raise RenderError(f"unsafe match text {value!r}")
    return value


def hostname(value: str) -> str:
    """Return ``value`` if it is a lower-case hostname, else raise.

    Example:
        >>> hostname("app.example.test")
        'app.example.test'
        >>> hostname("*.example.test")
        Traceback (most recent call last):
            ...
        switchyard.render.escape.RenderError: unsafe hostname '*.example.test'
    """
    if not _HOST.fullmatch(value) or len(value) > 253:
        raise RenderError(f"unsafe hostname {value!r}")
    return value


def wildcard_regex(pattern: str) -> str:
    """Return an anchored regex matching exactly one label in place of ``*``.

    Dots are written as ``[.]`` so the regex contains no backslash, which HAProxy would read as an
    escape character in a configuration word.

    Example:
        >>> wildcard_regex("*.example.test")
        '^[^.]+[.]example[.]test$'
    """
    if not pattern.startswith("*."):
        raise RenderError(f"not a wildcard pattern {pattern!r}")
    return "^[^.]+[.]" + hostname(pattern[2:]).replace(".", "[.]") + "$"


def bind_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int) -> str:
    """Return the ``address:port`` form HAProxy parses unambiguously for either IP family.

    Example:
        >>> bind_address(ipaddress.ip_address("192.0.2.20"), 443)
        '192.0.2.20:443'
        >>> bind_address(ipaddress.ip_address("2001:db8::20"), 443)
        'ipv6@2001:db8::20:443'
    """
    if not 1 <= port <= 65535:
        raise RenderError(f"port out of range: {port}")
    if address.version == 6:
        return f"ipv6@{address}:{port}"
    return f"{address}:{port}"
