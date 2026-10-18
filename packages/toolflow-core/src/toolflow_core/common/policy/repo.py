"""Bounded process-local manager caching with stable live-object identity."""

from collections.abc import Callable
from time import monotonic

from cachetools import TTLCache

from .base import BasePolicy, BasePolicyManager, BasePolicyManagerRepo
from .store import PolicyStore
from .types import StoredPolicy


class CachetoolsPolicyManagerRepo[TPolicy: BasePolicy](BasePolicyManagerRepo[TPolicy]):
    """Retain managers in a TTL/LRU cache and reuse all surviving references.

    TTL controls strong-reference retention, not policy freshness. After expiry
    or eviction, a surviving manager is recached without a source read. Once all
    references disappear, a subsequent get may reconstruct from durable state.
    Use ``reload`` for explicit source refresh. The store is shared with every
    constructed manager, so updates always go back to the same authority.

    All access is confined to the repo's event loop. Cache operations below do
    not suspend, so no OS-thread lock or worker thread is necessary. Do not
    expose or access the cache directly from another thread.
    """

    def __init__(
        self,
        store: PolicyStore[TPolicy],
        *,
        ttl: float = 60.0,
        maxsize: int = 1024,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        super().__init__(store, ttl=ttl)
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        self._cache: TTLCache[str, BasePolicyManager[TPolicy]] = TTLCache(
            maxsize=maxsize, ttl=ttl, timer=timer
        )

    async def _read_cache(self, key: str) -> BasePolicyManager[TPolicy] | None:
        # Expired entries can still retain values physically. Release them before
        # consulting the weak registry, or expired cache references look "live".
        self._cache.expire()
        return self._cache.get(key)

    async def _read_source(self, key: str) -> StoredPolicy[TPolicy] | None:
        return await self._store.load(key)

    async def _write_cache(self, key: str, manager: BasePolicyManager[TPolicy]) -> None:
        self._cache[key] = manager
