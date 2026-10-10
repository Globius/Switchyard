"""Errors and problem reports raised by the domain layer."""

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Problem:
    """One validation finding, addressed by a field path so a UI can mark the exact input.

    Attributes:
        path: Dotted path to the offending object or field, for example
            ``virtual_services.web.ports[1].port``.
        code: Stable machine-readable identifier, for example ``vip_outside_network``.
        message: Human-readable explanation.
    """

    path: str
    code: str
    message: str


class DomainValidationError(ValueError):
    """Raised when a snapshot fails domain validation.

    Attributes:
        problems: Every finding, sorted so the output is deterministic.
    """

    def __init__(self, problems: list[Problem]) -> None:
        """Build the error from a non-empty list of problems."""
        self.problems = sorted(problems)
        lines = "\n".join(f"  {p.path}: {p.message} [{p.code}]" for p in self.problems)
        super().__init__(f"{len(self.problems)} validation problem(s):\n{lines}")
