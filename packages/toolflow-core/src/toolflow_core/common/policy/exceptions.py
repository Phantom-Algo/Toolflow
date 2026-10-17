"""Errors raised by the policy core."""

from collections.abc import Iterable, Mapping


class PolicyError(Exception):
    """Base class for policy errors."""


class PolicyValidationError(PolicyError, ValueError):
    """Configuration does not satisfy its domain model."""

    def __init__(
        self, message: str, *, errors: Iterable[Mapping[str, object]] | None = None
    ) -> None:
        super().__init__(message)
        self._errors = [dict(error) for error in errors] if errors is not None else []

    def errors(self) -> list[dict[str, object]]:
        """Return field-level validation details, when available."""
        return [dict(error) for error in self._errors]


class PolicyImmutabilityError(PolicyError, TypeError):
    """A declaration or value cannot be represented as immutable policy data."""


class PolicyConflictError(PolicyError):
    """The supplied snapshot is not the manager's current snapshot."""

    def __init__(self, *, current_revision: int) -> None:
        super().__init__(
            f"Policy changed or snapshot belongs to another manager "
            f"(current revision: {current_revision})"
        )
        self.current_revision = current_revision


class PolicyLoadError(PolicyError):
    """A configuration source could not be read or decoded."""
