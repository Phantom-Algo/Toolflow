"""Repository contract and default cache unit tests."""

import asyncio

import pytest

from policy_test_support import Clock, MemoryPolicyStore, checkpoint, make_manager, run_async
from toolflow_core.common.policy import (
    BasePolicy,
    BasePolicyManagerRepo,
    CachetoolsPolicyManagerRepo,
    PolicyCacheError,
    PolicyManagerNotFoundError,
    PolicyValidationError,
)


class RuntimePolicy(BasePolicy):
    value: int = 0


class RecordingRepo(BasePolicyManagerRepo[RuntimePolicy]):
    """An independent TTL cache adapter exercising the abstract template."""
    def __init__(self, store):
        super().__init__(store, ttl=10)
        self.clock = Clock()
        self.entries = {}
        self.events = []
        self.failure = None
        self.write_entered = asyncio.Event()
        self.write_release = None

    async def _read_cache(self, key):
        self.events.append("cache")
        if self.failure == "cache":
            raise OSError("cache read failed")
        if key in self.entries:
            expires, manager = self.entries[key]
            if self.clock() < expires:
                return manager
            del self.entries[key]
        return None

    async def _read_source(self, key):
        self.events.append("source")
        return await self._store.load(key)

    async def _write_cache(self, key, manager):
        self.events.append("write")
        if self.failure == "write":
            raise OSError("cache write failed")
        self.entries[key] = (self.clock() + self._ttl, manager)
        self.write_entered.set()
        if self.write_release is not None:
            await self.write_release.wait()


@run_async
async def test_template_calls_hooks_in_order_and_reuses_a_hit():
    store = MemoryPolicyStore({"agent": RuntimePolicy()})
    repo = RecordingRepo(store)
    manager = await repo.get("agent")
    assert repo.events == ["cache", "source", "write"]
    repo.events.clear()
    assert await repo.get("agent") is manager
    assert repo.events == ["cache"]
    assert store.loads == ["agent"]


@run_async
async def test_expired_custom_cache_recaches_a_live_object_without_source_io():
    store = MemoryPolicyStore({"agent": RuntimePolicy()})
    repo = RecordingRepo(store)
    manager = await repo.get("agent")
    repo.clock.advance(10)
    repo.events.clear()
    assert await repo.get("agent") is manager
    assert repo.events == ["cache", "write"]
    assert store.loads == ["agent"]


@run_async
async def test_missing_source_is_not_negatively_cached():
    store = MemoryPolicyStore()
    repo = RecordingRepo(store)
    for _ in range(2):
        with pytest.raises(PolicyManagerNotFoundError) as caught:
            await repo.get("missing")
        assert caught.value.key == "missing"
    assert store.loads == ["missing", "missing"]
    assert not repo.entries
    store.seed("missing", RuntimePolicy())
    assert await repo.get("missing")


@pytest.mark.parametrize("stage", ["cache", "source", "write"])
@run_async
async def test_hook_errors_propagate_and_release_the_key_lock(stage):
    store = MemoryPolicyStore({"agent": RuntimePolicy()})
    repo = RecordingRepo(store)
    repo.failure = stage
    if stage == "source":
        store.load_error = OSError("source read failed")
    with pytest.raises(OSError):
        await repo.get("agent")
    assert repo.events == {
        "cache": ["cache"], "source": ["cache", "source"],
        "write": ["cache", "source", "write"],
    }[stage]
    repo.failure = None
    store.load_error = None
    assert await repo.get("agent")


@run_async
async def test_cache_cannot_return_a_copied_or_foreign_manager():
    store = MemoryPolicyStore({"agent": RuntimePolicy()})
    repo = RecordingRepo(store)
    original = await repo.get("agent")
    repo.entries["agent"] = (10, make_manager(RuntimePolicy()))
    with pytest.raises(PolicyCacheError):
        await repo.get("agent")
    assert (await original.snapshot()).policy.value == 0


@run_async
async def test_cancellation_during_load_does_not_leave_a_partial_registration():
    store = MemoryPolicyStore({"agent": RuntimePolicy()})
    store.load_release = asyncio.Event()
    repo = RecordingRepo(store)
    pending = asyncio.create_task(repo.get("agent"))
    await store.load_started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert "agent" not in repo._live
    store.load_release.set()
    assert await repo.get("agent")
    assert store.loads == ["agent", "agent"]


@run_async
async def test_cancellation_after_cache_write_preserves_registered_identity():
    store = MemoryPolicyStore({"agent": RuntimePolicy()})
    repo = RecordingRepo(store)
    repo.write_release = asyncio.Event()
    pending = asyncio.create_task(repo.get("agent"))
    await repo.write_entered.wait()
    cached = repo.entries["agent"][1]
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    repo.write_release.set()
    assert await repo.get("agent") is cached
    assert store.loads == ["agent"]


@run_async
async def test_concurrent_first_gets_construct_one_manager_and_read_source_once():
    store = MemoryPolicyStore({"agent": RuntimePolicy()})
    store.load_release = asyncio.Event()
    repo = CachetoolsPolicyManagerRepo(store)
    requests = [asyncio.create_task(repo.get("agent")) for _ in range(30)]
    await store.load_started.wait()
    await checkpoint()
    assert store.loads == ["agent"] and not any(r.done() for r in requests)
    store.load_release.set()
    managers = await asyncio.gather(*requests)
    assert all(m is managers[0] for m in managers)
    assert store.loads == ["agent"]


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), True, "60", None])
def test_default_cache_rejects_invalid_ttl(ttl):
    with pytest.raises(ValueError):
        CachetoolsPolicyManagerRepo(MemoryPolicyStore(), ttl=ttl)


@pytest.mark.parametrize("size", [0, -1, 1.5, True])
def test_default_cache_rejects_invalid_capacity(size):
    with pytest.raises(ValueError):
        CachetoolsPolicyManagerRepo(MemoryPolicyStore(), maxsize=size)


@pytest.mark.parametrize("key", ["", None, 123])
@run_async
async def test_repo_rejects_invalid_keys_before_accessing_adapters(key):
    repo = RecordingRepo(MemoryPolicyStore())
    with pytest.raises(ValueError):
        await repo.get(key)
    assert not repo.events


def test_repo_rejects_a_second_event_loop_even_on_an_uncontended_cache_hit():
    repo = CachetoolsPolicyManagerRepo(MemoryPolicyStore({"k": RuntimePolicy()}))
    asyncio.run(repo.get("k"))
    with pytest.raises(RuntimeError, match="one event loop"):
        asyncio.run(repo.get("k"))


@run_async
async def test_invalid_source_record_is_not_cached():
    store = MemoryPolicyStore({"k": RuntimePolicy()})
    record = store.records["k"]
    store.records["k"] = type(record)(record.policy, "")
    repo = RecordingRepo(store)
    with pytest.raises(PolicyValidationError):
        await repo.get("k")
    assert not repo.entries and "k" not in repo._live
