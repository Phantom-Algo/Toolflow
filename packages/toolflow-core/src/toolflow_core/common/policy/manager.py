"""Single-process snapshot publication with optimistic conflict detection."""

from collections.abc import Callable
from threading import Lock

from .base import BasePolicy
from .exceptions import PolicyConflictError, PolicyValidationError
from .types import PolicySnapshot


class PolicyManager[TPolicy: BasePolicy]:
    """Share this manager; acquire one snapshot per logical business operation."""

    def __init__(self, initial: TPolicy) -> None:
        if not isinstance(initial, BasePolicy):
            raise PolicyValidationError("initial must be a BasePolicy instance")
        self._policy_type = type(initial)
        isolated = self._prepare(initial)
        self._lock = Lock()
        self._current = PolicySnapshot(revision=0, policy=isolated)

    def _prepare(self, candidate: TPolicy) -> TPolicy:
        if type(candidate) is not self._policy_type:
            raise PolicyValidationError(
                f"Expected {self._policy_type.__name__}, got {type(candidate).__name__}"
            )
        # Reconstruct rather than trusting model_construct/model_copy or aliases.
        return self._policy_type.from_config(candidate.to_config())

    def snapshot(self) -> PolicySnapshot[TPolicy]:
        """Read a complete snapshot without holding the lock during its use."""
        with self._lock:
            current = self._current
        return current

    def replace(
        self,
        candidate: TPolicy,
        *,
        expected: PolicySnapshot[TPolicy],
    ) -> PolicySnapshot[TPolicy]:
        """Publish only if ``expected`` is still the actual current snapshot.

        Validation and copying run outside the publication lock. A conflict
        never retries and never changes the current state.
        """
        isolated = self._prepare(candidate)
        with self._lock:
            previous = self._current
            if previous is not expected:
                raise PolicyConflictError(current_revision=previous.revision)
            published = PolicySnapshot(revision=previous.revision + 1, policy=isolated)
            self._current = published
        return published

    def update(
        self,
        transform: Callable[[TPolicy], TPolicy],
        *,
        expected: PolicySnapshot[TPolicy],
    ) -> PolicySnapshot[TPolicy]:
        """Apply a pure transform exactly once outside the lock, then publish."""
        current = self.snapshot()
        if current is not expected:
            raise PolicyConflictError(current_revision=current.revision)
        candidate = transform(expected.policy)
        return self.replace(candidate, expected=expected)
