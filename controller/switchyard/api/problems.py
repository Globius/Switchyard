"""RFC 9457 ``application/problem+json`` errors.

Every error response has ``type``, ``title``, ``status``, ``detail`` and, for validation failures,
``errors``: a list of ``{path, code, message}`` where ``path`` addresses the exact input so a UI can
mark the field (backend design, "Conventions").
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from switchyard.db.store import ConcurrentModificationError
from switchyard.domain.changes import (
    OperationInvalidError,
    StaleVersionError,
    UnknownObjectError,
)
from switchyard.domain.errors import DomainValidationError
from switchyard.services.changesets import ValidationFailedError
from switchyard.services.errors import (
    AlreadyExistsError,
    ConflictError,
    FieldErrors,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
)

PROBLEM = "application/problem+json"
_TITLES = {
    400: "Bad request",
    401: "Authentication required",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    412: "Precondition failed",
    422: "Validation failed",
    428: "Precondition required",
    500: "Internal server error",
}


class ApiError(Exception):
    """An error with a ready-made HTTP status, raised by route code."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        errors: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Create the error.

        Args:
            status: HTTP status.
            detail: Human-readable explanation.
            errors: Field-level findings for validation failures.
            headers: Extra response headers.
            code: Stable machine-readable ``code`` member (``session_expired``, ``csrf`` ...).
            extra: More problem members, for example ``required_role``.
        """
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.errors = errors
        self.headers = headers or {}
        self.code = code
        self.extra = extra or {}


def problem(
    status: int,
    detail: str,
    *,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
    code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a problem+json response.

    ``code`` is a stable identifier clients can switch on; 401 and 403 problems always carry one
    (``session_expired``, ``csrf``, ``role_required`` with ``required_role``, ...).
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
    }
    if code:
        body["code"] = code
    if extra:
        body.update(extra)
    if errors:
        body["errors"] = errors
    if extra:
        body |= extra
    return JSONResponse(body, status_code=status, media_type=PROBLEM, headers=headers)


def _errors(problems: Any) -> list[dict[str, str]]:
    return [{"path": p.path, "code": p.code, "message": p.message} for p in problems]


Handler = Callable[[Request, Any], Awaitable[JSONResponse]]

#: ``kind`` values of the discriminated unions (policy sets, routing profiles). Pydantic puts the
#: tag in the error location; it is not a field, so it is dropped from ``errors[].path``.
UNION_TAGS = frozenset(
    {
        "redirect",
        "allow_list",
        "rewrite",
        "respond",
        "error_pages",
        "vrrp",
        "bgp",
        "oidc",
        "saml",
    }
)


#: Tags of the rewrite-action union; Pydantic puts one right after the ``action`` field.
ACTION_TAGS = frozenset({"set_header", "add_header", "remove_header", "replace_prefix"})


def strip_union_tag(loc: tuple[Any, ...]) -> str:
    """Turn a Pydantic error location into the dotted ``errors[].path`` of the API.

    The leading ``body``/``query``/``path`` part is dropped, and so is the discriminator tag that a
    union adds right after ``body`` or after a rewrite rule's ``action``.

    Example:
        >>> strip_union_tag(("body", "vrrp", "vrid"))
        'vrid'
        >>> strip_union_tag(("body", "rewrite", "rules", 0, "action", "set_header", "value"))
        'rules.0.action.value'
        >>> strip_union_tag(("query", "limit"))
        'limit'
    """
    parts = list(loc[1:])
    if loc and loc[0] == "body" and len(parts) > 1 and parts[0] in UNION_TAGS:
        parts = parts[1:]
    parts = [p for i, p in enumerate(parts) if not (p in ACTION_TAGS and parts[i - 1] == "action")]
    return ".".join(str(p) for p in parts) or str(loc[0])


def install_handlers(app: FastAPI) -> None:
    """Register exception handlers that produce problem+json."""

    def add(exc_type: type[Exception], build: Callable[[Any], JSONResponse]) -> None:
        async def handle(request: Request, exc: Any) -> JSONResponse:
            return build(exc)

        app.add_exception_handler(exc_type, handle)

    add(
        ApiError,
        lambda e: problem(
            e.status, e.detail, errors=e.errors, headers=e.headers, code=e.code, extra=e.extra
        ),
    )
    add(NotFoundError, lambda e: problem(404, str(e)))
    add(UnknownObjectError, lambda e: problem(404, str(e)))
    add(
        PermissionDeniedError,
        lambda e: problem(
            403,
            str(e),
            code="role_required" if e.required_role else "forbidden",
            extra={"required_role": e.required_role} if e.required_role else None,
        ),
    )
    add(AlreadyExistsError, lambda e: problem(409, str(e)))
    add(ConflictError, lambda e: problem(409, str(e)))
    add(ConcurrentModificationError, lambda e: problem(409, str(e)))
    add(
        StaleVersionError,
        lambda e: problem(
            412,
            str(e),
            extra={
                "conflict": {
                    "object_type": e.object_type.value,
                    "object_id": str(e.object_id),
                    "expected_version": e.expected,
                    "current_version": e.actual,
                }
            },
        ),
    )
    add(PreconditionFailedError, lambda e: problem(412, str(e)))
    add(
        OperationInvalidError,
        lambda e: problem(422, "an operation is invalid", errors=_errors(e.problems)),
    )
    add(
        ValidationFailedError,
        lambda e: problem(
            422, "the proposed state breaks domain rules", errors=_errors(e.problems)
        ),
    )
    add(
        DomainValidationError,
        lambda e: problem(422, "the state breaks domain rules", errors=_errors(e.problems)),
    )
    add(FieldErrors, lambda e: problem(422, str(e), errors=e.errors))
    add(ValueError, lambda e: problem(422, str(e)))

    async def validation(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):  # pragma: no cover
            raise exc
        errors = [
            {
                "path": strip_union_tag(tuple(err["loc"])),
                "code": str(err["type"]),
                "message": str(err["msg"]),
            }
            for err in exc.errors()
        ]
        return problem(422, "the request is invalid", errors=errors)

    app.add_exception_handler(RequestValidationError, validation)

    async def http(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
            raise exc
        return problem(exc.status_code, str(exc.detail), headers=dict(exc.headers or {}))

    app.add_exception_handler(StarletteHTTPException, http)
