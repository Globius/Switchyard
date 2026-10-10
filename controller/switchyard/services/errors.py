"""Errors raised by the service layer."""


class NotFoundError(LookupError):
    """A named tenant, SE group or other object does not exist (or is not visible)."""


class AlreadyExistsError(ValueError):
    """An object that would be created already exists."""
