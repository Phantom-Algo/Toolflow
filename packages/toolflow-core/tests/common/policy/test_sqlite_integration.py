"""Exercise a real disk-backed CAS source through the public repo/manager API."""

import asyncio
import gc
import runpy
from pathlib import Path
from weakref import ref

import pytest

from policy_test_support import Clock, run_async
from toolflow_core.common.policy import (
    CachetoolsPolicyManagerRepo,
    PolicySnapshot,
    PolicySourceConflictError,
    PolicyUnavailableError,
)

# Keep the example executable while verifying its adapter against real I/O.
example = runpy.run_path(str(Path(__file__).parents[3] / "examples/policy_snapshot.py"))
SQLitePolicyStore = example["SQLitePolicyStore"]
RuntimePolicy = example["RuntimePolicy"]


@run_async
async def test_committed_policy_survives_manager_collection_and_source_reopening(sqlite_policy_path):
    path = sqlite_policy_path
    store = SQLitePolicyStore(path)
    await store.initialize("agent", RuntimePolicy())
    await store.initialize("other", RuntimePolicy())
    clock = Clock()
    repo = CachetoolsPolicyManagerRepo(store, ttl=10, timer=clock)
    manager = await repo.get("agent")
    original = await manager.snapshot()
    published = await manager.update(lambda p: p.with_changes(timeout_ms=5000), expected=original)
    independent_reader = SQLitePolicyStore(path)
    persisted = await independent_reader.load("agent")
    assert persisted.policy == published.policy
    assert persisted.source_version == published.source_version
    weak_manager = ref(manager)
    del manager
    clock.advance(10)
    await repo.get("other")
    gc.collect()
    assert weak_manager() is None
    rebuilt = await repo.get("agent")
    snapshot = await rebuilt.snapshot()
    assert snapshot.revision == 0
    assert snapshot.policy.timeout_ms == 5000
    assert snapshot.source_version == persisted.source_version


@run_async
async def test_independent_repos_compete_using_database_cas_without_lost_updates(sqlite_policy_path):
    path = sqlite_policy_path
    store_a, store_b = SQLitePolicyStore(path), SQLitePolicyStore(path)
    await store_a.initialize("agent", RuntimePolicy())
    repo_a, repo_b = CachetoolsPolicyManagerRepo(store_a), CachetoolsPolicyManagerRepo(store_b)
    a, b = await asyncio.gather(repo_a.get("agent"), repo_b.get("agent"))
    base_a, base_b = await asyncio.gather(a.snapshot(), b.snapshot())
    assert base_a.source_version == base_b.source_version
    outcomes = await asyncio.gather(
        a.replace(RuntimePolicy(timeout_ms=1), expected=base_a),
        b.replace(RuntimePolicy(timeout_ms=2), expected=base_b),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PolicySnapshot) for r in outcomes) == 1
    assert sum(isinstance(r, PolicySourceConflictError) for r in outcomes) == 1
    winner = next(r for r in outcomes if isinstance(r, PolicySnapshot))
    persisted = await store_a.load("agent")
    assert persisted.policy == winner.policy and persisted.source_version == winner.source_version
    loser_repo, loser_manager = (repo_b, b) if isinstance(outcomes[1], Exception) else (repo_a, a)
    with pytest.raises(PolicyUnavailableError):
        await loser_manager.snapshot()
    assert await loser_repo.reload("agent") is loser_manager
    assert (await loser_manager.snapshot()).source_version == winner.source_version
