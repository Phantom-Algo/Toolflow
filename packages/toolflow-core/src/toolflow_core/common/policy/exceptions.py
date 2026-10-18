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


class PolicyManagerNotFoundError(PolicyError, LookupError):
    """No persisted policy exists for the requested manager key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"No policy found for {key!r}")
        self.key = key


class PolicyCacheError(PolicyError):
    """A cache adapter violated the manager identity contract."""


class PolicySourceConflictError(PolicyError):
    """The source record was removed or its version no longer matches."""

    def __init__(self, key: str, *, expected_source_version: str) -> None:
        super().__init__(
            f"Source policy changed or disappeared for {key!r} "
            f"(expected version: {expected_source_version!r})"
        )
        self.key = key
        self.expected_source_version = expected_source_version


class PolicyWriteRejectedError(PolicyError):
    """Persistence confirms no commit occurred and no write remains in flight."""


class PolicyWriteUncertainError(PolicyError):
    """Persistence cannot establish whether a write committed or is in flight."""


class PolicyUnavailableError(PolicyError):
    """A manager is blocked until its source state has been reconciled."""

    def __init__(self, key: str, *, uncertain: bool) -> None:
        action = "recover with a write resolver" if uncertain else "reload the source"
        super().__init__(f"Policy {key!r} is unavailable; {action}")
        self.key = key
        self.uncertain = uncertain
