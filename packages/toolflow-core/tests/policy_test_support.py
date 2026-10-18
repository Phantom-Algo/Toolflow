"""Deterministic policy test adapters; these stores do not provide durability."""

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from functools import wraps
from typing import Any
from uuid import uuid4

from toolflow_core.common.policy import (
    BasePolicy,
    PolicyManager,
    PolicySourceConflictError,
    StoredPolicy,
)


def run_async(function: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    """Run an async pytest test without a plugin, bounding waits and deadlocks."""
    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(asyncio.wait_for(function(*args, **kwargs), timeout=10))
    return run


async def checkpoint() -> None:
    """Let already-scheduled tasks attempt their next step without timed sleeps."""
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MemoryPolicyStore[TPolicy: BasePolicy]:
    """A controlled CAS source with gates before loading or committing."""
    def __init__(self, initial: Mapping[str, TPolicy] | None = None) -> None:
        self.records: dict[str, StoredPolicy[TPolicy]] = {}
        for key, policy in (initial or {}).items():
            self.seed(key, policy)
        self.loads: list[str] = []
        self.saves: list[str] = []
        self.load_started = asyncio.Event()
        self.save_started = asyncio.Event()
        self.load_release: asyncio.Event | None = None
        self.save_release: asyncio.Event | None = None
        self.load_error: BaseException | None = None
        self.save_error: BaseException | None = None
        self.commit_before_error = False

    def seed(self, key: str, policy: TPolicy) -> StoredPolicy[TPolicy]:
        record = StoredPolicy(policy, uuid4().hex)
        self.records[key] = record
        return record

    async def load(self, key: str) -> StoredPolicy[TPolicy] | None:
        self.loads.append(key)
        self.load_started.set()
        if self.load_release is not None:
            await asyncio.wait_for(self.load_release.wait(), timeout=5)
        if self.load_error is not None:
            raise self.load_error
        return self.records.get(key)

    async def save(
        self, key: str, candidate: TPolicy, *, expected_source_version: str
    ) -> str:
        self.saves.append(key)
        self.save_started.set()
        if self.save_release is not None:
            await asyncio.wait_for(self.save_release.wait(), timeout=5)
        current = self.records.get(key)
        if current is None or current.source_version != expected_source_version:
            raise PolicySourceConflictError(
                key, expected_source_version=expected_source_version
            )
        if self.save_error is not None and not self.commit_before_error:
            raise self.save_error
        # No suspension between version comparison and record replacement.
        committed = self.seed(key, candidate)
        if self.save_error is not None:
            raise self.save_error
        return committed.source_version


def make_manager[TPolicy: BasePolicy](policy: TPolicy) -> PolicyManager[TPolicy]:
    store = MemoryPolicyStore({"policy": policy})
    return PolicyManager(store.records["policy"], key="policy", store=store)
