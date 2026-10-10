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
