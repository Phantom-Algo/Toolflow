"""Asynchronous, durable publication through a stable manager reference."""

from __future__ import annotations

from asyncio import Lock
from collections.abc import Callable

from ._runtime import LoopAffinity, finish_before_unlock
from .base import BasePolicy, BasePolicyManager
from .exceptions import (
    PolicyConflictError,
    PolicySourceConflictError,
    PolicyUnavailableError,
    PolicyValidationError,
    PolicyWriteRejectedError,
)
from .store import PolicyStore
from .types import PolicySnapshot, StoredPolicy


class PolicyManager[TPolicy: BasePolicy](BasePolicyManager[TPolicy]):
    """Persist changes before publishing, excluding readers during each commit.

    Usually obtain instances from a shared repo. Direct construction requires
    an already-loaded source record and a real store; there is no implicit
    memory-only writer. ``_``-prefixed arguments are repository coordination
    details, not persistence extension points.
    """

    def __init__(
        self,
        initial: StoredPolicy[TPolicy],
        *,
        key: str,
        store: PolicyStore[TPolicy],
        _lock: Lock | None = None,
        _affinity: LoopAffinity | None = None,
        _on_unavailable: Callable[[PolicyManager[TPolicy]], None] | None = None,
        _on_recovered: Callable[[PolicyManager[TPolicy]], None] | None = None,
    ) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("policy key must be a non-empty string")
        if not isinstance(initial, StoredPolicy) or not isinstance(
            initial.policy, BasePolicy
        ):
            raise PolicyValidationError("initial must contain a BasePolicy instance")
        self._validate_version(initial.source_version)
        self._policy_type = type(initial.policy)
        isolated = self._prepare(initial.policy)
        self._key = key
        self._store = store
        self._lock = _lock if _lock is not None else Lock()
        self._affinity = _affinity if _affinity is not None else LoopAffinity()
        self._on_unavailable = _on_unavailable
        self._on_recovered = _on_recovered
        self._blocked_reason: BaseException | None = None
        self._uncertain = False
        self._current = PolicySnapshot(
            revision=0, policy=isolated, source_version=initial.source_version
        )

    @property
    def key(self) -> str:
        """The immutable identity used for all source reads and writes."""
        return self._key

    @staticmethod
    def _validate_version(version: str) -> None:
        if not isinstance(version, str) or not version:
            raise PolicyValidationError("source_version must be a non-empty string")

    def _prepare(self, candidate: TPolicy) -> TPolicy:
        if type(candidate) is not self._policy_type:
            raise PolicyValidationError(
                f"Expected {self._policy_type.__name__}, got {type(candidate).__name__}"
            )
        # Reconstruct rather than trusting model_construct/model_copy or aliases.
        return self._policy_type.from_config(candidate.to_config())

    def _ensure_available(self) -> None:
        if self._blocked_reason is not None:
            raise PolicyUnavailableError(
                self._key, uncertain=self._uncertain
            ) from self._blocked_reason

    def _ensure_reloadable(self) -> None:
        if self._uncertain:
            self._ensure_available()

    def _mark_unavailable(self, reason: BaseException, *, uncertain: bool) -> None:
        self._blocked_reason = reason
        self._uncertain = uncertain
        if self._on_unavailable is not None:
            self._on_unavailable(self)

    def _install_loaded(self, stored: StoredPolicy[TPolicy]) -> None:
        """Install a reconciled source record; the repo must already hold the lock."""
        self._validate_version(stored.source_version)
        isolated = self._prepare(stored.policy)
        previous = self._current
        if stored.source_version == previous.source_version:
            if isolated != previous.policy:
                raise PolicyValidationError("Source reused a version for different data")
            # Preserve outstanding expected-snapshot credentials on no-op reloads.
        else:
            self._current = PolicySnapshot(
                revision=previous.revision + 1,
                policy=isolated,
                source_version=stored.source_version,
            )
        self._blocked_reason = None
        self._uncertain = False
        if self._on_recovered is not None:
            self._on_recovered(self)

    async def snapshot(self) -> PolicySnapshot[TPolicy]:
        """Wait for commits, then acquire one immutable snapshot per operation.

        Previously returned snapshots remain usable; they are not revoked by
        later updates. No lock is retained while business code uses a snapshot.
        """
        self._affinity.check()
        async with self._lock:
            self._ensure_available()
            return self._current

    async def replace(
        self,
        candidate: TPolicy,
        *,
        expected: PolicySnapshot[TPolicy],
    ) -> PolicySnapshot[TPolicy]:
        """Check the local snapshot and source version, commit, then publish.

        Validation runs before locking. Once submission starts, cancellation
        waits for the save/publication operation to finish before releasing the
        lock. Cancellation therefore does not imply that nothing was committed.
        """
        self._affinity.check()
        isolated = self._prepare(candidate)
        async with self._lock:
            self._ensure_available()
            if self._current is not expected:
                raise PolicyConflictError(current_revision=self._current.revision)
            return await finish_before_unlock(self._persist_and_publish(isolated))

    async def _persist_and_publish(self, candidate: TPolicy) -> PolicySnapshot[TPolicy]:
        previous = self._current
        try:
            version = await self._store.save(
                self._key,
                candidate,
                expected_source_version=previous.source_version,
            )
            self._validate_version(version)
            if version == previous.source_version:
                raise PolicyValidationError("A successful save must return a fresh version")
            # There is no suspension point between confirmed commit and publication.
            published = PolicySnapshot(
                revision=previous.revision + 1, policy=candidate, source_version=version
            )
            self._current = published
            return published
        except PolicyWriteRejectedError:
            # The adapter guarantees that no write remains in flight.
            raise
        except PolicySourceConflictError as exc:
            self._mark_unavailable(exc, uncertain=False)
            raise
        except BaseException as exc:
            # Unknown errors (including internal cancellation) cannot prove rollback.
            self._mark_unavailable(exc, uncertain=True)
            raise

    async def update(
        self,
        transform: Callable[[TPolicy], TPolicy],
        *,
        expected: PolicySnapshot[TPolicy],
    ) -> PolicySnapshot[TPolicy]:
        """Run a synchronous pure transform once, then persist conditionally.

        Transforms must not perform blocking I/O. A concurrent publication may
        invalidate the baseline; no transform or persistence retry is hidden.
        """
        current = await self.snapshot()
        if current is not expected:
            raise PolicyConflictError(current_revision=current.revision)
        candidate = transform(expected.policy)
        return await self.replace(candidate, expected=expected)
