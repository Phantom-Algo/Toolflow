"""Manager unit tests: validation, durable ordering, CAS and cancellation."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest
from pydantic import Field

from policy_test_support import MemoryPolicyStore, checkpoint, make_manager, run_async
from toolflow_core.common.policy import (
    BasePolicy,
    PolicyConflictError,
    PolicyManager,
    PolicySnapshot,
    PolicySourceConflictError,
    PolicyUnavailableError,
    PolicyValidationError,
    PolicyWriteRejectedError,
    PolicyWriteUncertainError,
    StoredPolicy,
)


class RuntimePolicy(BasePolicy):
    timeout_ms: int = Field(default=1000, gt=0)


class OtherPolicy(BasePolicy):
    enabled: bool = True


@run_async
async def test_initialization_and_publication_isolate_and_revalidate_candidates():
    candidate = RuntimePolicy()
    store = MemoryPolicyStore({"agent": candidate})
    manager = PolicyManager(store.records["agent"], key="agent", store=store)
    base = await manager.snapshot()
    assert base.policy is not candidate
    published = await manager.replace(candidate, expected=base)
    assert published.policy is not candidate
    assert published.revision == 1 and base.revision == 0
    assert published.source_version == store.records["agent"].source_version
    assert published.source_version != base.source_version
    assert await manager.snapshot() is published
    assert manager.key == "agent"
    with pytest.raises(AttributeError):
        manager.key = "another-agent"
    with pytest.raises(FrozenInstanceError):
        published.revision = 100


@pytest.mark.parametrize("kind", ["construct", "copy", "unknown", "incomplete"])
@run_async
async def test_unvalidated_candidates_cannot_bypass_checks(kind):
    if kind == "construct":
        candidate = RuntimePolicy.model_construct(timeout_ms=0)
    elif kind == "copy":
        candidate = RuntimePolicy().model_copy(update={"timeout_ms": 0})
    elif kind == "unknown":
        candidate = RuntimePolicy().model_copy(update={"unknown": 1})
    else:
        candidate = RuntimePolicy.model_construct()
        candidate.__dict__.pop("timeout_ms")
    with pytest.raises(PolicyValidationError):
        make_manager(candidate)
    manager = make_manager(RuntimePolicy())
    base = await manager.snapshot()
    with pytest.raises(PolicyValidationError):
        await manager.replace(candidate, expected=base)
    assert await manager.snapshot() is base


@run_async
async def test_wrong_domain_type_and_invalid_record_rejected():
    manager = make_manager(RuntimePolicy())
    base = await manager.snapshot()
    with pytest.raises(PolicyValidationError):
        await manager.replace(OtherPolicy(), expected=base)
    with pytest.raises(PolicyValidationError):
        PolicyManager(StoredPolicy({}, "v0"), key="k", store=MemoryPolicyStore())
    with pytest.raises(PolicyValidationError):
        PolicyManager(StoredPolicy(RuntimePolicy(), ""), key="k", store=MemoryPolicyStore())
    assert await manager.snapshot() is base


@run_async
async def test_old_snapshot_is_stable_and_same_value_save_advances_versions():
    manager = make_manager(RuntimePolicy())
    old = await manager.snapshot()
    current = await manager.update(lambda p: p.with_changes(timeout_ms=2), expected=old)
    same = await manager.replace(current.policy, expected=current)
    assert old.policy.timeout_ms == 1000 and current.policy.timeout_ms == 2
    assert same.revision == 2 and same is not current
    assert same.policy == current.policy
    assert len({old.source_version, current.source_version, same.source_version}) == 3


@run_async
async def test_foreign_forged_and_stale_snapshots_never_write_source():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    foreign = await make_manager(RuntimePolicy()).snapshot()
    forged = PolicySnapshot(0, base.policy, base.source_version)
    for expected in (foreign, forged):
        with pytest.raises(PolicyConflictError):
            await manager.replace(base.policy, expected=expected)
    assert not store.saves
    published = await manager.replace(base.policy, expected=base)
    with pytest.raises(PolicyConflictError) as caught:
        await manager.replace(base.policy, expected=base)
    assert caught.value.current_revision == 1
    assert store.saves == ["k"]
    assert await manager.snapshot() is published


@run_async
async def test_transform_runs_once_and_not_for_a_stale_baseline():
    manager = make_manager(RuntimePolicy())
    base = await manager.snapshot()
    calls = []

    def fail(policy):
        calls.append(policy)
        raise RuntimeError("business failure")

    with pytest.raises(RuntimeError, match="business failure"):
        await manager.update(fail, expected=base)
    assert await manager.snapshot() is base
    await manager.replace(base.policy, expected=base)
    with pytest.raises(PolicyConflictError):
        await manager.update(fail, expected=base)
    assert calls == [base.policy]


@run_async
async def test_two_writers_using_one_base_have_exactly_one_durable_winner():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    results = await asyncio.gather(
        manager.replace(RuntimePolicy(timeout_ms=1), expected=base),
        manager.replace(RuntimePolicy(timeout_ms=2), expected=base),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PolicySnapshot) for r in results) == 1
    assert sum(isinstance(r, PolicyConflictError) for r in results) == 1
    assert store.saves == ["k"]
    snapshot = await manager.snapshot()
    assert snapshot.revision == 1
    assert snapshot.policy == store.records["k"].policy


@run_async
async def test_readers_and_other_writers_wait_for_confirmed_commit():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    store.save_release = asyncio.Event()
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    writer = asyncio.create_task(manager.replace(RuntimePolicy(timeout_ms=2), expected=base))
    await store.save_started.wait()
    reader = asyncio.create_task(manager.snapshot())
    contender = asyncio.create_task(manager.replace(RuntimePolicy(timeout_ms=3), expected=base))
    await checkpoint()
    assert not reader.done() and not contender.done()
    assert manager._current is base
    assert store.records["k"].source_version == base.source_version
    # The event loop can still execute unrelated work while persistence waits.
    assert await make_manager(RuntimePolicy()).snapshot()
    store.save_release.set()
    published = await writer
    assert await reader is published
    with pytest.raises(PolicyConflictError):
        await contender
    assert store.saves == ["k"]


@run_async
async def test_candidate_validation_runs_before_acquiring_the_commit_lock():
    manager = make_manager(RuntimePolicy())
    base = await manager.snapshot()
    async with manager._lock:
        with pytest.raises(PolicyValidationError):
            await manager.replace(RuntimePolicy.model_construct(timeout_ms=0), expected=base)


@run_async
async def test_confirmed_rejection_preserves_snapshot_and_allows_retry():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    store.save_error = PolicyWriteRejectedError("transaction rolled back")
    with pytest.raises(PolicyWriteRejectedError):
        await manager.replace(RuntimePolicy(timeout_ms=2), expected=base)
    assert await manager.snapshot() is base
    assert store.records["k"].source_version == base.source_version
    store.save_error = None
    assert (await manager.replace(RuntimePolicy(timeout_ms=2), expected=base)).revision == 1


@pytest.mark.parametrize("error", [
    PolicyWriteUncertainError("lost commit acknowledgement"),
    RuntimeError("unexpected driver failure"),
    asyncio.CancelledError(),
])
@run_async
async def test_uncertain_or_unclassified_failure_blocks_future_access(error):
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    store.save_error = error
    with pytest.raises(type(error)):
        await manager.replace(RuntimePolicy(timeout_ms=2), expected=base)
    assert manager._current is base
    for operation in (manager.snapshot(), manager.replace(base.policy, expected=base)):
        with pytest.raises(PolicyUnavailableError) as caught:
            await operation
        assert caught.value.uncertain


@run_async
async def test_source_version_conflict_cannot_overwrite_newer_data():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    external = store.seed("k", RuntimePolicy(timeout_ms=99))
    with pytest.raises(PolicySourceConflictError):
        await manager.replace(RuntimePolicy(timeout_ms=2), expected=base)
    assert store.records["k"] is external
    with pytest.raises(PolicyUnavailableError) as caught:
        await manager.snapshot()
    assert not caught.value.uncertain


@run_async
async def test_cancellation_while_waiting_for_lock_has_no_source_effect():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    async with manager._lock:
        writer = asyncio.create_task(manager.replace(RuntimePolicy(timeout_ms=2), expected=base))
        await checkpoint()
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert not store.saves
    assert await manager.snapshot() is base


@run_async
async def test_repeated_cancellation_during_commit_waits_for_publication():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    store.save_release = asyncio.Event()
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    writer = asyncio.create_task(manager.replace(RuntimePolicy(timeout_ms=2), expected=base))
    await store.save_started.wait()
    reader = asyncio.create_task(manager.snapshot())
    for _ in range(3):
        writer.cancel()
        await checkpoint()
        assert not writer.done() and not reader.done()
        assert manager._current is base
    store.save_release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer
    published = await reader
    assert published.policy.timeout_ms == 2
    assert published.source_version == store.records["k"].source_version


@pytest.mark.parametrize("error", [PolicyWriteRejectedError("rollback"), RuntimeError("driver")])
@run_async
async def test_commit_failure_takes_precedence_over_delayed_caller_cancellation(error):
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    store.save_release = asyncio.Event()
    store.save_error = error
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    writer = asyncio.create_task(manager.replace(RuntimePolicy(timeout_ms=2), expected=base))
    await store.save_started.wait()
    writer.cancel()
    await checkpoint()
    store.save_release.set()
    with pytest.raises(type(error)):
        await writer
    assert not manager._lock.locked()


@pytest.mark.parametrize("returned", ["", "same", None])
@run_async
async def test_invalid_save_version_is_treated_as_uncertain(returned):
    class InvalidStore(MemoryPolicyStore):
        async def save(self, key, candidate, *, expected_source_version):
            await super().save(key, candidate, expected_source_version=expected_source_version)
            return expected_source_version if returned == "same" else returned
    store = InvalidStore({"k": RuntimePolicy()})
    manager = PolicyManager(store.records["k"], key="k", store=store)
    base = await manager.snapshot()
    with pytest.raises(PolicyValidationError):
        await manager.replace(RuntimePolicy(timeout_ms=2), expected=base)
    with pytest.raises(PolicyUnavailableError):
        await manager.snapshot()


def test_manager_rejects_a_second_event_loop_even_without_lock_contention():
    manager = make_manager(RuntimePolicy())
    asyncio.run(manager.snapshot())
    with pytest.raises(RuntimeError, match="one event loop"):
        asyncio.run(manager.snapshot())


@run_async
async def test_nested_unvalidated_policy_cannot_bypass_manager_validation():
    class Threshold(BasePolicy):
        limit: int = Field(gt=0)
    class NestedPolicy(BasePolicy):
        threshold: Threshold
    invalid = NestedPolicy.model_construct(threshold=Threshold.model_construct(limit=0))
    with pytest.raises(PolicyValidationError):
        make_manager(invalid)
    manager = make_manager(NestedPolicy(threshold=Threshold(limit=1)))
    base = await manager.snapshot()
    with pytest.raises(PolicyValidationError):
        await manager.replace(invalid, expected=base)
    assert await manager.snapshot() is base
