"""Run with ``uv run --package toolflow-core python <this file>`` from repo root.

This small SQLite adapter demonstrates real persistence and source-side CAS.
It is an application example, not a production schema or migration framework.
Each worker owns its connection; no synchronous database I/O runs on the loop.
"""

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic import Field

from toolflow_core.common.policy import (
    BasePolicy,
    CachetoolsPolicyManagerRepo,
    PolicyConflictError,
    PolicySourceConflictError,
    PolicyWriteUncertainError,
    StoredPolicy,
)


class RuntimePolicy(BasePolicy):
    timeout_ms: int = Field(default=3000, gt=0)
    allowed_tools: frozenset[str] = frozenset()
    tool_timeouts: Mapping[str, int] = Field(default_factory=dict)


class SQLitePolicyStore:
    """A complete-policy adapter using SQLite transactions and opaque versions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self, key: str, initial: RuntimePolicy) -> None:
        """Create demo storage and seed a missing record without overwriting it."""
        await asyncio.to_thread(self._initialize, key, initial)

    def _initialize(self, key: str, initial: RuntimePolicy) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS policies "
                    "(policy_key TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                    "source_version TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO policies VALUES (?, ?, ?)",
                    (key, initial.model_dump_json(), uuid4().hex),
                )

    async def load(self, key: str) -> StoredPolicy[RuntimePolicy] | None:
        return await asyncio.to_thread(self._load, key)

    def _load(self, key: str) -> StoredPolicy[RuntimePolicy] | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT payload, source_version FROM policies WHERE policy_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return StoredPolicy(RuntimePolicy.from_config(json.loads(row[0])), row[1])

    async def save(
        self,
        key: str,
        candidate: RuntimePolicy,
        *,
        expected_source_version: str,
    ) -> str:
        return await asyncio.to_thread(self._save, key, candidate, expected_source_version)

    def _save(
        self, key: str, candidate: RuntimePolicy, expected_source_version: str
    ) -> str:
        version = uuid4().hex
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                with connection:
                    changed = connection.execute(
                        "UPDATE policies SET payload = ?, source_version = ? "
                        "WHERE policy_key = ? AND source_version = ?",
                        (candidate.model_dump_json(), version, key, expected_source_version),
                    ).rowcount
                    if changed != 1:
                        raise PolicySourceConflictError(
                            key, expected_source_version=expected_source_version
                        )
            # Return only after the transaction context confirms commit.
            return version
        except sqlite3.Error as exc:
            # Conservatively require reconciliation instead of guessing rollback.
            raise PolicyWriteUncertainError("SQLite write outcome needs verification") from exc


async def main() -> None:
    with TemporaryDirectory(prefix="toolflow-policy-") as directory:
        store = SQLitePolicyStore(Path(directory) / "policy.sqlite3")
        await store.initialize(
            "user-1/agent-1", RuntimePolicy(allowed_tools=frozenset({"search"}))
        )
        repo = CachetoolsPolicyManagerRepo(store)
        manager = await repo.get("user-1/agent-1")
        assert await repo.get("user-1/agent-1") is manager
        old = await manager.snapshot()
        published = await manager.update(
            lambda policy: policy.with_changes(timeout_ms=5000), expected=old
        )
        print(f"Old snapshot: revision={old.revision}, timeout={old.policy.timeout_ms}")
        print(f"Published: revision={published.revision}, timeout={published.policy.timeout_ms}")
        try:
            await manager.replace(old.policy, expected=old)
        except PolicyConflictError:
            print("Stale update rejected; published policy is unchanged.")
        persisted = await store.load("user-1/agent-1")
        assert persisted is not None and persisted.policy == published.policy
        assert persisted.source_version == published.source_version
        print(f"Persisted before publication: timeout={persisted.policy.timeout_ms}")


if __name__ == "__main__":
    asyncio.run(main())
