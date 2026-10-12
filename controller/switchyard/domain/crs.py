# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A catalogue of OWASP Core Rule Set rules for the WAF screen (ADR 0140, 0152).

The catalogue lists the rule *families* of CRS 4.x and the rules tenants most often switch off
because of false positives. It is **not** the whole rule set: any rule id may be put in a WAF
policy's ``disabled_rule_ids``, listed here or not. It exists so a user can pick a rule by what it
does instead of copying a number out of an audit log.

Example:
    >>> group_of(942100).name
    'SQL injection'
    >>> find(920350).name
    'Host header is a numeric IP address'
"""

from dataclasses import dataclass

#: The CRS release the catalogue describes (the one a WAF policy selects).
CRS_VERSION = "4.8.0"


@dataclass(frozen=True)
class CrsRule:
    """One rule.

    Attributes:
        id: The rule id as the audit log shows it.
        name: What the rule detects, in a few words.
        paranoia_level: The lowest paranoia level at which the rule is active (1 to 4).
    """

    id: int
    name: str
    paranoia_level: int


@dataclass(frozen=True)
class CrsGroup:
    """A family of rules that share the first three digits of their ids.

    Attributes:
        prefix: The three-digit id prefix (``942`` covers ids 942000 to 942999).
        name: The family.
        description: What the family protects against.
        rules: The commonly met rules of the family.
    """

    prefix: int
    name: str
    description: str
    rules: tuple[CrsRule, ...] = ()


def _r(rule_id: int, name: str, level: int = 1) -> CrsRule:
    return CrsRule(rule_id, name, level)


CATALOGUE: tuple[CrsGroup, ...] = (
    CrsGroup(
        911,
        "Method enforcement",
        "Refuses HTTP methods the application does not use.",
        (_r(911100, "Method is not allowed by policy"),),
    ),
    CrsGroup(
        913,
        "Scanner detection",
        "Recognises security scanners and crawlers by user agent and headers.",
        (_r(913100, "Found user agent associated with a security scanner"),),
    ),
    CrsGroup(
        920,
        "Protocol enforcement",
        "Requests that break HTTP rules or look unlike a real client.",
        (
            _r(920100, "Invalid HTTP request line"),
            _r(920120, "Attempted multipart/form-data bypass"),
            _r(920160, "Content-Length header is not numeric"),
            _r(920170, "GET or HEAD request with a body"),
            _r(920230, "Multiple URL encoding detected", 2),
            _r(920270, "Invalid character in request (null character)"),
            _r(920280, "Request is missing a Host header"),
            _r(920300, "Request is missing an Accept header", 3),
            _r(920320, "Missing User-Agent header", 3),
            _r(920350, "Host header is a numeric IP address"),
            _r(920420, "Request content type is not allowed by policy"),
            _r(920430, "HTTP protocol version is not allowed by policy"),
            _r(920440, "URL file extension is restricted by policy"),
        ),
    ),
    CrsGroup(
        921,
        "Protocol attack",
        "Request smuggling, response splitting and header injection.",
        (_r(921110, "HTTP request smuggling attack"),),
    ),
    CrsGroup(
        922,
        "Multipart attack",
        "Malformed multipart bodies.",
        (_r(922100, "Multipart request has an invalid character set"),),
    ),
    CrsGroup(
        930,
        "Local file inclusion",
        "Path traversal and access to files outside the web root.",
        (
            _r(930100, "Path traversal attack (/../) in the request path"),
            _r(930110, "Path traversal attack (/../) in the request body or query"),
            _r(930120, "OS file access attempt"),
            _r(930130, "Restricted file access attempt"),
        ),
    ),
    CrsGroup(
        931,
        "Remote file inclusion",
        "Parameters that make the application fetch a file from another server.",
        (_r(931100, "Possible remote file inclusion: URL parameter with an IP address", 2),),
    ),
    CrsGroup(
        932,
        "Remote command execution",
        "Operating-system command injection.",
        (
            _r(932100, "Unix command injection"),
            _r(932105, "Unix command injection: command found"),
            _r(932110, "Windows command injection"),
        ),
    ),
    CrsGroup(
        933,
        "PHP injection",
        "PHP code and wrapper abuse.",
        (_r(933100, "PHP injection attack: opening or closing tag found"),),
    ),
    CrsGroup(
        934,
        "Generic application attack",
        "Node.js injection, server-side request forgery and similar.",
        (_r(934100, "Node.js injection attack"),),
    ),
    CrsGroup(
        941,
        "Cross-site scripting",
        "Script injection into pages.",
        (
            _r(941100, "XSS attack detected via libinjection"),
            _r(941110, "XSS filter: script tag vector"),
        ),
    ),
    CrsGroup(
        942,
        "SQL injection",
        "SQL injection into queries.",
        (
            _r(942100, "SQL injection attack detected via libinjection"),
            _r(942130, "SQL injection attack: SQL tautology detected", 2),
        ),
    ),
    CrsGroup(
        943,
        "Session fixation",
        "Attempts to set a victim's session id.",
        (_r(943100, "Possible session fixation: cookie set through HTML"),),
    ),
    CrsGroup(
        944,
        "Java attack",
        "Java deserialisation, expression-language and class abuse.",
        (_r(944100, "Remote command execution: suspicious Java class detected"),),
    ),
    CrsGroup(
        949,
        "Inbound blocking evaluation",
        "Adds up the request's anomaly score and decides whether to block.",
        (_r(949110, "Inbound anomaly score exceeded"),),
    ),
    CrsGroup(950, "Data leakages", "Responses that reveal internals."),
    CrsGroup(951, "SQL data leakages", "Database error messages in responses."),
    CrsGroup(952, "Java data leakages", "Java stack traces and source in responses."),
    CrsGroup(953, "PHP data leakages", "PHP errors and source in responses."),
    CrsGroup(954, "IIS data leakages", "IIS errors and source in responses."),
    CrsGroup(
        959,
        "Outbound blocking evaluation",
        "Adds up the response's anomaly score and decides whether to block.",
        (_r(959100, "Outbound anomaly score exceeded"),),
    ),
    CrsGroup(980, "Correlation", "Summarises the anomaly scores of a request in the log."),
)


def group_of(rule_id: int) -> CrsGroup | None:
    """The family a rule id belongs to, or ``None`` for an id outside the CRS ranges."""
    return next((g for g in CATALOGUE if rule_id // 1000 == g.prefix), None)


def find(rule_id: int) -> CrsRule | None:
    """The catalogue entry of a rule id, or ``None`` when it is not one of the listed rules."""
    group = group_of(rule_id)
    return next((r for r in group.rules if r.id == rule_id), None) if group else None
