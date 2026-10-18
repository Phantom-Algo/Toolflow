"""Repo/manager integration under controlled concurrency and external threads."""

import asyncio
import gc
import json
from threading import Barrier
from weakref import ref

import pytest

from policy_test_support import Clock, MemoryPolicyStore, checkpoint, run_async
from toolflow_core.common.policy import (
    BasePolicy,
    CachetoolsPolicyManagerRepo,
    PolicyConflictError,
    PolicyManagerNotFoundError,
    PolicySnapshot,
    PolicySourceConflictError,
    PolicyUnavailableError,
    PolicyValidationError,
    PolicyWriteUncertainError,
)


class PairPolicy(BasePolicy):
    generation: int = 0
    doubled: int = 0


@run_async
async def test_sessions_share_agent_identity_and_user_namespaces_remain_isolated():
    # Session is intentionally absent from the agent-manager identity.
    agent_key = json.dumps(["user-1", "agent-1"])
    other_user_key = json.dumps(["user-2", "agent-1"])
    store = MemoryPolicyStore({agent_key: PairPolicy(), other_user_key: PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)

    async def from_session(session_id):
        return session_id, await repo.get(agent_key)

    sessions = await asyncio.gather(from_session("session-A"), from_session("session-B"))
    manager_a, manager_b = sessions[0][1], sessions[1][1]
    assert manager_a is manager_b
    assert await repo.get(other_user_key) is not manager_a
    base = await manager_a.snapshot()
    published = await manager_a.replace(PairPolicy(generation=1, doubled=2), expected=base)
    assert await manager_b.snapshot() is published
    assert store.records[agent_key].policy == published.policy


@run_async
async def test_ttl_expiration_preserves_live_identity_and_does_not_refresh_source():
    clock = Clock()
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store, ttl=10, timer=clock)
    manager = await repo.get("agent")
    base = await manager.snapshot()
    store.seed("agent", PairPolicy(generation=5, doubled=10))
    clock.advance(10)  # Exact expiration boundary, without sleeping.
    assert await repo.get("agent") is manager
    assert await manager.snapshot() is base
    assert store.loads == ["agent"]
    assert await repo.reload("agent") is manager
    refreshed = await manager.snapshot()
    assert refreshed.policy.generation == 5 and refreshed.revision == 1
    assert refreshed.source_version == store.records["agent"].source_version


@run_async
async def test_capacity_eviction_preserves_a_surviving_manager_reference():
    store = MemoryPolicyStore({"a": PairPolicy(), "b": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store, maxsize=1)
    a = await repo.get("a")
    b = await repo.get("b")
    assert await repo.get("a") is a
    assert await repo.get("b") is b
    assert store.loads == ["a", "b"]


@run_async
async def test_physical_expiration_releases_unreferenced_managers_and_reloads_durable_value():
    clock = Clock()
    store = MemoryPolicyStore({"agent": PairPolicy(), "other": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store, ttl=10, timer=clock)
    manager = await repo.get("agent")
    old = await manager.snapshot()
    published = await manager.replace(PairPolicy(generation=1, doubled=2), expected=old)
    weak_manager = ref(manager)
    del manager
    clock.advance(10)
    await repo.get("other")  # Also causes physical cleanup of expired strong refs.
    gc.collect()
    assert weak_manager() is None
    rebuilt = await repo.get("agent")
    snapshot = await rebuilt.snapshot()
    assert snapshot.revision == 0
    assert snapshot.policy == published.policy
    assert snapshot.source_version == published.source_version
    assert store.loads == ["agent", "other", "agent"]
    assert len(repo._locks) == 64


@run_async
async def test_cache_hits_and_manager_updates_do_not_extend_original_ttl():
    clock = Clock()
    store = MemoryPolicyStore({"agent": PairPolicy(), "other": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store, ttl=10, timer=clock)
    manager = await repo.get("agent")
    clock.advance(9)
    assert await repo.get("agent") is manager
    base = await manager.snapshot()
    await manager.replace(PairPolicy(generation=1, doubled=2), expected=base)
    weak_manager = ref(manager)
    del manager
    clock.advance(1)
    await repo.get("other")
    gc.collect()
    assert weak_manager() is None


@run_async
async def test_ttl_get_and_explicit_reload_wait_for_an_inflight_save():
    clock = Clock()
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store, ttl=10, timer=clock)
    manager = await repo.get("agent")
    base = await manager.snapshot()
    store.save_release = asyncio.Event()
    writer = asyncio.create_task(manager.replace(PairPolicy(generation=1, doubled=2), expected=base))
    await store.save_started.wait()
    clock.advance(10)
    getter = asyncio.create_task(repo.get("agent"))
    reloader = asyncio.create_task(repo.reload("agent"))
    reader = asyncio.create_task(manager.snapshot())
    await checkpoint()
    assert not any(t.done() for t in (getter, reloader, reader))
    assert store.loads == ["agent"]
    store.save_release.set()
    published = await writer
    assert await getter is manager and await reloader is manager
    assert await reader is published
    assert await manager.snapshot() is published  # Same-version reload is a no-op.
    assert store.loads == ["agent", "agent"]


@run_async
async def test_slow_key_does_not_block_an_unrelated_lock_segment():
    store = MemoryPolicyStore({"slow": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)
    other_key = next(str(i) for i in range(1000) if repo._lock_for(str(i)) is not repo._lock_for("slow"))
    store.seed(other_key, PairPolicy())
    slow = await repo.get("slow")
    other = await repo.get(other_key)
    store.save_release = asyncio.Event()
    writer = asyncio.create_task(slow.replace(PairPolicy(generation=1, doubled=2), expected=await slow.snapshot()))
    await store.save_started.wait()
    assert await repo.get(other_key) is other
    assert (await other.snapshot()).policy.generation == 0
    store.save_release.set()
    await writer


@run_async
async def test_source_conflict_blocks_until_reload_and_preserves_manager_identity():
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)
    manager = await repo.get("agent")
    base = await manager.snapshot()
    external = store.seed("agent", PairPolicy(generation=5, doubled=10))
    with pytest.raises(PolicySourceConflictError):
        await manager.replace(PairPolicy(generation=1, doubled=2), expected=base)
    with pytest.raises(PolicyUnavailableError):
        await repo.get("agent")
    assert await repo.reload("agent") is manager
    snapshot = await manager.snapshot()
    assert snapshot.policy == external.policy and snapshot.revision == 1
    with pytest.raises(PolicyConflictError):
        await manager.replace(base.policy, expected=base)
    assert not repo._blocked


@pytest.mark.parametrize("did_commit", [False, True])
@run_async
async def test_uncertain_writes_survive_ttl_and_require_explicit_reconciliation(did_commit):
    clock = Clock()
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store, ttl=10, timer=clock)
    manager = await repo.get("agent")
    base = await manager.snapshot()
    store.save_error = PolicyWriteUncertainError("lost acknowledgement")
    store.commit_before_error = did_commit
    with pytest.raises(PolicyWriteUncertainError):
        await manager.replace(PairPolicy(generation=1, doubled=2), expected=base)
    clock.advance(10)
    for operation in (repo.get("agent"), repo.reload("agent"), manager.snapshot()):
        with pytest.raises(PolicyUnavailableError) as caught:
            await operation
        assert caught.value.uncertain
    assert store.loads == ["agent"]
    assert repo._blocked["agent"] is manager

    resolving, resolved = asyncio.Event(), asyncio.Event()
    async def resolve(key):
        # A real adapter waits for/queries the original transaction here.
        resolving.set()
        await resolved.wait()
        return store.records[key]
    recovery = asyncio.create_task(repo.recover("agent", resolve))
    await resolving.wait()
    reader = asyncio.create_task(manager.snapshot())
    await checkpoint()
    assert not reader.done()
    resolved.set()
    assert await recovery is manager
    snapshot = await reader
    assert snapshot.policy.generation == int(did_commit)
    assert snapshot.revision == int(did_commit)
    assert (snapshot is base) == (not did_commit)
    assert not repo._blocked


@run_async
async def test_blocked_record_is_retained_even_when_callers_drop_the_manager():
    store = MemoryPolicyStore({"agent": PairPolicy(), "other": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store, maxsize=1)
    async def fail_and_release():
        manager = await repo.get("agent")
        base = await manager.snapshot()
        store.save_error = PolicyWriteUncertainError("unknown")
        try:
            await manager.replace(PairPolicy(generation=1, doubled=2), expected=base)
        except PolicyWriteUncertainError:
            pass
        store.save_error = None
        return ref(manager)
    weak_manager = await fail_and_release()
    await repo.get("other")
    gc.collect()
    assert weak_manager() is repo._blocked["agent"]
    with pytest.raises(PolicyUnavailableError):
        await repo.get("agent")
    recovered = await repo.recover("agent", store.load)
    assert recovered is weak_manager()
    assert not repo._blocked


@run_async
async def test_failed_or_cancelled_recovery_does_not_unblock_the_key():
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)
    manager = await repo.get("agent")
    store.save_error = PolicyWriteUncertainError("unknown")
    with pytest.raises(PolicyWriteUncertainError):
        await manager.replace(PairPolicy(generation=1, doubled=2), expected=await manager.snapshot())
    async def fail(key):
        raise OSError("could not establish transaction outcome")
    with pytest.raises(OSError):
        await repo.recover("agent", fail)
    entered = asyncio.Event()
    async def pending(key):
        entered.set()
        await asyncio.Event().wait()
    recovery = asyncio.create_task(repo.recover("agent", pending))
    await entered.wait()
    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    with pytest.raises(PolicyUnavailableError):
        await repo.get("agent")


@run_async
async def test_reload_handles_deleted_source_without_serving_old_handles():
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)
    manager = await repo.get("agent")
    del store.records["agent"]
    with pytest.raises(PolicyManagerNotFoundError):
        await repo.reload("agent")
    with pytest.raises(PolicyUnavailableError):
        await manager.snapshot()
    store.seed("agent", PairPolicy(generation=2, doubled=4))
    assert await repo.reload("agent") is manager
    assert (await manager.snapshot()).policy.generation == 2


@run_async
async def test_invalid_reload_preserves_snapshot_and_same_version_cannot_change_data():
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)
    manager = await repo.get("agent")
    base = await manager.snapshot()
    record = store.records["agent"]
    store.records["agent"] = type(record)(PairPolicy(generation=2, doubled=4), record.source_version)
    with pytest.raises(PolicyValidationError, match="reused a version"):
        await repo.reload("agent")
    assert await manager.snapshot() is base


@run_async
async def test_many_concurrent_readers_observe_complete_retained_snapshots():
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)
    manager = await repo.get("agent")
    phase = asyncio.Barrier(4)
    async def reader():
        retained = []
        for _ in range(30):
            await phase.wait()
            shared = await repo.get("agent")
            assert shared is manager
            snapshot = await shared.snapshot()
            assert snapshot.policy.doubled == snapshot.policy.generation * 2
            retained.append(snapshot)
            await phase.wait()
        assert all(s.policy.generation == s.revision for s in retained)
    async def writer():
        for generation in range(1, 31):
            await phase.wait()
            await manager.replace(
                PairPolicy(generation=generation, doubled=generation * 2),
                expected=await manager.snapshot(),
            )
            await phase.wait()
    await asyncio.gather(writer(), *(reader() for _ in range(3)))
    assert len(store.saves) == 30
    assert (await manager.snapshot()).policy == store.records["agent"].policy


@run_async
async def test_external_threads_share_identity_and_compete_via_the_owner_loop():
    store = MemoryPolicyStore({"agent": PairPolicy()})
    repo = CachetoolsPolicyManagerRepo(store)
    loop = asyncio.get_running_loop()
    rendezvous = Barrier(2, timeout=5)
    def external_writer(generation):
        manager = asyncio.run_coroutine_threadsafe(repo.get("agent"), loop).result(timeout=5)
        base = asyncio.run_coroutine_threadsafe(manager.snapshot(), loop).result(timeout=5)
        rendezvous.wait()  # Both OS threads retain the same baseline.
        operation = manager.replace(PairPolicy(generation=generation, doubled=generation * 2), expected=base)
        try:
            return manager, asyncio.run_coroutine_threadsafe(operation, loop).result(timeout=5)
        except PolicyConflictError as exc:
            return manager, exc
    a, b = await asyncio.gather(asyncio.to_thread(external_writer, 1), asyncio.to_thread(external_writer, 2))
    assert a[0] is b[0] is await repo.get("agent")
    assert sum(isinstance(outcome, PolicySnapshot) for _, outcome in (a, b)) == 1
    assert sum(isinstance(outcome, PolicyConflictError) for _, outcome in (a, b)) == 1
    assert len(store.loads) == len(store.saves) == 1
    assert (await a[0].snapshot()).policy == store.records["agent"].policy


@run_async
async def test_late_write_stays_quarantined_until_the_original_operation_finishes():
    class DeferredStore(MemoryPolicyStore):
        def __init__(self):
            super().__init__({"agent": PairPolicy()})
            self.finish_original = asyncio.Event()
            self.original = None
        async def save(self, key, candidate, *, expected_source_version):
            async def commit_later():
                await asyncio.wait_for(self.finish_original.wait(), timeout=5)
                return await super(DeferredStore, self).save(
                    key, candidate, expected_source_version=expected_source_version
                )
            # Model a remote/worker write whose acknowledgement was lost while
            # the actual operation remains live after the request has returned.
            self.original = asyncio.create_task(commit_later())
            raise PolicyWriteUncertainError("original write is still running")
    store = DeferredStore()
    clock = Clock()
    repo = CachetoolsPolicyManagerRepo(store, ttl=10, timer=clock)
    manager = await repo.get("agent")
    base = await manager.snapshot()
    with pytest.raises(PolicyWriteUncertainError):
        await manager.replace(PairPolicy(generation=1, doubled=2), expected=base)
    assert store.records["agent"].policy.generation == 0
    clock.advance(10)
    with pytest.raises(PolicyUnavailableError):
        await repo.reload("agent")
    assert store.loads == ["agent"]

    resolving = asyncio.Event()
    async def settle_and_load(key):
        resolving.set()
        await asyncio.shield(store.original)
        return await store.load(key)
    recovery = asyncio.create_task(repo.recover("agent", settle_and_load))
    await resolving.wait()
    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert not store.original.done()
    with pytest.raises(PolicyUnavailableError):
        await repo.get("agent")
    resolving.clear()
    recovery = asyncio.create_task(repo.recover("agent", settle_and_load))
    await resolving.wait()
    reader = asyncio.create_task(manager.snapshot())
    await checkpoint()
    assert not reader.done() and store.records["agent"].policy.generation == 0
    store.finish_original.set()
    assert await recovery is manager
    snapshot = await reader
    assert snapshot.policy.generation == 1
    assert snapshot.source_version == store.records["agent"].source_version
    assert not repo._blocked
