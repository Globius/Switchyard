"""Errors raised by the service layer."""


class NotFoundError(LookupError):
    """A named or numbered object does not exist (or is not visible to the caller)."""


class AlreadyExistsError(ValueError):
    """An object that would be created already exists."""


class ConflictError(RuntimeError):
    """The request conflicts with the current state (wrong lifecycle state, stale version)."""


class PermissionDeniedError(PermissionError):
    """The principal's role or tenant scope does not allow the action."""
