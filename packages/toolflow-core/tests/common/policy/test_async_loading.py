"""Async loading, cancellation, and publication against the pre-await snapshot."""

import asyncio
from threading import Event

import pytest
from pydantic import Field

from toolflow_core.common.policy import (
    BasePolicy,
    PolicyConflictError,
    PolicyLoadError,
    PolicyManager,
    reload_policy,
)


class RuntimePolicy(BasePolicy):
    timeout_ms: int = Field(default=1000, gt=0)


def run_scenario(coroutine):
    """Bound event-based async tests without requiring a pytest asyncio plugin."""
    async def bounded():
        async with asyncio.timeout(5):
            await coroutine

    asyncio.run(bounded())


def test_async_io_yields_and_detects_updates_while_waiting():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        manager = PolicyManager(RuntimePolicy())
        base = manager.snapshot()

        class Loader:
            async def load(self):
                entered.set()
                await release.wait()
                return {"timeout_ms": 2}

        task = asyncio.create_task(reload_policy(manager, Loader(), RuntimePolicy.from_config))
        await entered.wait()
        assert not task.done()

        async def consume_and_update():
            assert manager.snapshot() is base
            return manager.update(lambda p: p.with_changes(timeout_ms=3), expected=base)

        winner = await asyncio.create_task(consume_and_update())
        release.set()
        with pytest.raises(PolicyConflictError):
            await task
        assert manager.snapshot() is winner
        assert winner.policy.timeout_ms == 3

    run_scenario(scenario())


def test_two_concurrent_async_reloads_have_one_winner():
    async def scenario():
        manager = PolicyManager(RuntimePolicy())
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]

        class Loader:
            def __init__(self, index):
                self.index = index

            async def load(self):
                entered[self.index].set()
                await release[self.index].wait()
                return {"timeout_ms": self.index + 2}

        tasks = [
            asyncio.create_task(reload_policy(manager, Loader(i), RuntimePolicy.from_config))
            for i in range(2)
        ]
        await asyncio.gather(*(event.wait() for event in entered))
        release[1].set()
        winner = await tasks[1]
        release[0].set()
        with pytest.raises(PolicyConflictError):
            await tasks[0]
        assert manager.snapshot() is winner
        assert winner.revision == 1
        assert winner.policy.timeout_ms == 3

    run_scenario(scenario())


def test_cancellation_during_load_propagates_without_building_or_publication():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        manager = PolicyManager(RuntimePolicy())
        base = manager.snapshot()
        builds = []

        class Loader:
            async def load(self):
                entered.set()
                await release.wait()
                return {"timeout_ms": 2}

        def builder(raw):
            builds.append(raw)
            return RuntimePolicy.from_config(raw)

        task = asyncio.create_task(reload_policy(manager, Loader(), builder))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert task.cancelled()
        assert not builds
        assert manager.snapshot() is base

    run_scenario(scenario())


def test_cancelled_thread_io_cannot_publish_when_the_read_finishes_later():
    async def scenario():
        entered, release, finished = Event(), Event(), Event()
        manager = PolicyManager(RuntimePolicy())
        base = manager.snapshot()

        class Loader:
            def read(self):
                entered.set()
                assert release.wait(timeout=5)
                finished.set()
                return {"timeout_ms": 2}

            async def load(self):
                return await asyncio.to_thread(self.read)

        task = asyncio.create_task(reload_policy(manager, Loader(), RuntimePolicy.from_config))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        assert manager.snapshot() is base

    run_scenario(scenario())


def test_caller_timeout_preserves_current_snapshot():
    async def scenario():
        manager = PolicyManager(RuntimePolicy())
        base = manager.snapshot()

        class Loader:
            async def load(self):
                await asyncio.Event().wait()
                return {"timeout_ms": 2}

        with pytest.raises(TimeoutError):
            # A zero deadline cancels at the first suspension, without sleeps.
            async with asyncio.timeout(0):
                await reload_policy(manager, Loader(), RuntimePolicy.from_config)
        assert manager.snapshot() is base

    run_scenario(scenario())


def test_builder_exception_is_not_wrapped_as_a_load_error():
    async def scenario():
        manager = PolicyManager(RuntimePolicy())
        base = manager.snapshot()

        class Loader:
            async def load(self):
                return {"timeout_ms": 2}

        def builder(raw):
            raise RuntimeError("builder failed")

        with pytest.raises(RuntimeError, match="builder failed"):
            await reload_policy(manager, Loader(), builder)
        assert manager.snapshot() is base

    run_scenario(scenario())


def test_loader_policy_error_retains_identity():
    async def scenario():
        error = PolicyLoadError("source failed")
        manager = PolicyManager(RuntimePolicy())
        base = manager.snapshot()

        class Loader:
            async def load(self):
                raise error

        with pytest.raises(PolicyLoadError) as caught:
            await reload_policy(manager, Loader(), RuntimePolicy.from_config)
        assert caught.value is error
        assert manager.snapshot() is base

    run_scenario(scenario())


def test_legacy_sync_loader_must_be_migrated():
    async def scenario():
        manager = PolicyManager(RuntimePolicy())
        base = manager.snapshot()

        class LegacyLoader:
            def load(self):
                return {"timeout_ms": 2}

        with pytest.raises(PolicyLoadError) as caught:
            await reload_policy(manager, LegacyLoader(), RuntimePolicy.from_config)
        assert isinstance(caught.value.__cause__, TypeError)
        assert manager.snapshot() is base

    run_scenario(scenario())
