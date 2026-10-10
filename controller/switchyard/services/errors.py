"""Errors raised by the service layer."""


class NotFoundError(LookupError):
    """A named or numbered object does not exist (or is not visible to the caller)."""


class AlreadyExistsError(ValueError):
    """An object that would be created already exists."""


class ConflictError(RuntimeError):
    """The request conflicts with the current state (wrong lifecycle state, stale version)."""


class PermissionDeniedError(PermissionError):
    """The principal's role or tenant scope does not allow the action.

    ``required_role`` is set when the cause is a role that is too weak (the API reports it as
    ``code: role_required`` with a ``required_role`` member); it stays ``None`` for tenant scope.
    """

    def __init__(self, message: str = "", required_role: str | None = None) -> None:
        """Create the error."""
        super().__init__(message)
        self.required_role = required_role


class PreconditionFailedError(RuntimeError):
    """``If-Match`` does not match the object's current version (HTTP 412)."""


class FieldErrors(ValueError):
    """Invalid input, with the dotted path of every offending field (HTTP 422 ``errors[]``).

    Collect findings with :meth:`add` and call :meth:`raise_if_any` once, so a client sees all
    problems of a request at once.

    Example:
        >>> errors = FieldErrors()
        >>> errors.add("issuer_url", "invalid", "must be https")
        >>> bool(errors), errors.errors[0]["path"]
        (True, 'issuer_url')
    """

    def __init__(self) -> None:
        """Create an empty collection."""
        super().__init__("the request is invalid")
        self.errors: list[dict[str, str]] = []

    def add(self, path: str, code: str, message: str) -> None:
        """Record one finding."""
        self.errors.append({"path": path, "code": code, "message": message})

    def has(self, path: str) -> bool:
        """True if a finding exists for ``path``."""
        return any(e["path"] == path for e in self.errors)

    def __bool__(self) -> bool:
        """True once at least one finding was added."""
        return bool(self.errors)

    def raise_if_any(self) -> None:
        """Raise this error if there are findings."""
        if self.errors:
            raise self
