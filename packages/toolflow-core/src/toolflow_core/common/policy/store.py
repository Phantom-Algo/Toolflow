"""Persistence adapter contract shared by repositories and managers."""

from typing import Protocol

from .base import BasePolicy
from .types import StoredPolicy


class PolicyStore[TPolicy: BasePolicy](Protocol):
    """Read consistent records and commit complete policies with source-side CAS.

    ``save`` must atomically compare the source version, save a policy equivalent
    to the candidate, and return a fresh, non-empty version after durable commit.
    Versions must not be reused, including after deletion and recreation.
    A missing record or stale version raises ``PolicySourceConflictError``.

    Raise ``PolicyWriteRejectedError`` only when no commit occurred and no write
    remains in flight. Use ``PolicyWriteUncertainError`` when the outcome cannot
    be established. Other write exceptions are conservatively treated as
    uncertain. Synchronous drivers must be adapted without blocking the loop;
    cancellation of a worker-thread await does not prove the write stopped.

    Adapters own transactions, serialization and durability. They must support
    concurrent calls for different keys and must not reenter the same repo or
    manager while a policy lock is held.
    """

    async def load(self, key: str) -> StoredPolicy[TPolicy] | None:
        """Read a complete validated policy/version pair from the authority."""
        ...

    async def save(
        self,
        key: str,
        candidate: TPolicy,
        *,
        expected_source_version: str,
    ) -> str:
        """Commit conditionally and return the new source version."""
        ...
