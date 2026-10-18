"""Internal event-loop confinement and cancellation-safe commit helpers."""

import asyncio
from collections.abc import Coroutine
from typing import Any


class LoopAffinity:
    """Bind lazily so repositories can be configured before a loop is running."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    def check(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError(
                "Policy objects belong to one event loop; submit cross-thread "
                "calls with asyncio.run_coroutine_threadsafe"
            )


async def finish_before_unlock[T](operation: Coroutine[Any, Any, T]) -> T:
    """Delay caller cancellation until the complete commit operation finishes.

    The caller must hold the policy lock for this entire await. Shielding only
    the source write would let cancellation release the lock before the write
    or subsequent publication finishes. Keep a strong task reference and also
    handle repeated cancellation requests. An operation error takes precedence
    over delayed caller cancellation.
    """
    task = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()
    return result
