"""Policy domain models and manager contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from asyncio import Lock
from collections.abc import Awaitable, Callable, Mapping
from math import isfinite
from typing import TYPE_CHECKING, Self, cast, final
from weakref import WeakValueDictionary

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_serializer,
    model_validator,
)

from ._runtime import LoopAffinity
from .exceptions import PolicyImmutabilityError, PolicyValidationError

if TYPE_CHECKING:
    from .......docs.infrastructure.drawio.manager import PolicyManager
    from .store import PolicyStore
    from .types import PolicySnapshot, StoredPolicy


class BasePolicy(BaseModel):

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        validate_default=True,
        allow_inf_nan=False,
    )

    @classmethod
    def __pydantic_on_complete__(cls) -> None:
        super().__pydantic_on_complete__()
        from ._immutable import check_annotation

        required = {
            "frozen": True,
            "extra": "forbid",
            "revalidate_instances": "always",
            "validate_default": True,
            "allow_inf_nan": False,
        }
        if any(cls.model_config.get(key) != value for key, value in required.items()):
            raise PolicyImmutabilityError("Policy subclasses must preserve BasePolicy config")
        if cls.__private_attributes__ or cls.model_computed_fields:
            raise PolicyImmutabilityError("Policies cannot contain private or computed state")
        for name, field in cls.model_fields.items():
            check_annotation(field.annotation, f"{cls.__name__}.{name}")

    @model_validator(mode="after")
    def _freeze_fields(self) -> Self:
        from ._immutable import copy_tree

        for name in type(self).model_fields:
            object.__setattr__(
                self,
                name,
                copy_tree(getattr(self, name), frozen=True, path=name, active={id(self)}),
            )
        return self

    @field_serializer("*", mode="wrap", check_fields=False)
    def _serialize_field(
        self, value: object, handler: SerializerFunctionWrapHandler
    ) -> object:
        from ._immutable import serializer_value

        return handler(serializer_value(value))

    @classmethod
    def from_config(cls, raw: Mapping[str, object]) -> Self:
        """Validate an isolated configuration tree and freeze all containers."""
        from ._immutable import copy_tree

        if not isinstance(raw, Mapping):
            raise PolicyValidationError("Policy configuration must be a mapping")
        data = copy_tree(raw)
        try:
            return cls.model_validate(data, by_alias=True, by_name=True)
        except ValidationError as exc:
            raise PolicyValidationError(
                str(exc), errors=exc.errors(include_url=False)
            ) from exc

    def to_config(self) -> dict[str, object]:
        """Export detached ordinary containers using canonical field names."""
        from ._immutable import copy_tree

        return cast(dict[str, object], copy_tree(self, export=True))

    def with_changes(self, **changes: object) -> Self:
        """Replace top-level fields, then revalidate all domain constraints."""
        unknown = changes.keys() - type(self).model_fields.keys()
        if unknown:
            raise PolicyValidationError(f"Unknown policy fields: {sorted(unknown)}")
        data = self.to_config()
        data.update(changes)
        return type(self).from_config(data)


class BasePolicyManager[TPolicy: BasePolicy](ABC):
    """Async access to snapshots and durable publication on one event loop.

    Submit calls from other OS threads with ``asyncio.run_coroutine_threadsafe``.
    """

    @abstractmethod
    async def snapshot(self) -> PolicySnapshot[TPolicy]:
        """Wait for any active commit, then return the current snapshot."""
        ...

    @abstractmethod
    async def replace(
        self,
        candidate: TPolicy,
        *,
        expected: PolicySnapshot[TPolicy],
    ) -> PolicySnapshot[TPolicy]:
        """Persist and publish only if ``expected`` is still current."""
        ...

    @abstractmethod
    async def update(
        self,
        transform: Callable[[TPolicy], TPolicy],
        *,
        expected: PolicySnapshot[TPolicy],
    ) -> PolicySnapshot[TPolicy]:
        """Apply ``transform`` once and publish the result conditionally."""
        ...


class BasePolicyManagerRepo[TPolicy: BasePolicy](ABC):
    """Cache managers without splitting the identity of a live instance.

    Reuse one repo per policy type and source namespace in an application.
    Subclasses implement the three I/O hooks; the template owns construction,
    coordination and recovery. Cached values must be the original in-process
    manager objects, never serialized or copied managers.
    """

    def __init__(self, store: PolicyStore[TPolicy], *, ttl: float = 60.0) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not isfinite(ttl)
            or ttl <= 0
        ):
            raise ValueError("ttl must be finite and positive")
        self._store = store
        self._ttl = ttl
        self._affinity = LoopAffinity()
        # Bounded coordination storage; collisions only reduce concurrency.
        self._locks = tuple(Lock() for _ in range(64))
        self._live: WeakValueDictionary[str, PolicyManager[TPolicy]] = (
            WeakValueDictionary()
        )
        # Failed commits must survive TTL eviction and loss of caller references.
        self._blocked: dict[str, PolicyManager[TPolicy]] = {}

    def _lock_for(self, key: str) -> Lock:
        return self._locks[hash(key) % len(self._locks)]

    def _check_access(self, key: str) -> None:
        self._affinity.check()
        if not isinstance(key, str) or not key:
            raise ValueError("policy key must be a non-empty string")

    def _retain_blocked(self, manager: PolicyManager[TPolicy]) -> None:
        self._blocked[manager.key] = manager

    def _release_blocked(self, manager: PolicyManager[TPolicy]) -> None:
        self._blocked.pop(manager.key, None)

    @final
    async def get(self, key: str) -> BasePolicyManager[TPolicy]:
        """Return the same live manager, loading the source only when necessary.

        TTL releases a cache reference; it does not reload a policy. ``reload``
        explicitly observes external changes. Errors propagate; absence is not
        negatively cached. Hook implementations must not reenter this repo.
        """
        from .exceptions import PolicyCacheError, PolicyManagerNotFoundError

        self._check_access(key)
        async with self._lock_for(key):
            if blocked := self._blocked.get(key):
                blocked._ensure_available()
            cached = await self._read_cache(key)
            manager = self._live.get(key)
            if cached is not None and cached is not manager:
                raise PolicyCacheError(
                    "Cache returned an unregistered manager; preserve object identity"
                )
            if manager is not None:
                manager._ensure_available()
                if cached is None:
                    await self._write_cache(key, manager)
                return manager
            stored = await self._read_source(key)
            if stored is None:
                raise PolicyManagerNotFoundError(key)
            manager = self._create_manager(key, stored)
            # Register before an asynchronous cache write can yield or fail.
            self._live[key] = manager
            await self._write_cache(key, manager)
            return manager

    def _create_manager(
        self, key: str, stored: StoredPolicy[TPolicy]
    ) -> PolicyManager[TPolicy]:
        from .......docs.infrastructure.drawio.manager import PolicyManager

        return PolicyManager(
            stored,
            key=key,
            store=self._store,
            _lock=self._lock_for(key),
            _affinity=self._affinity,
            _on_unavailable=self._retain_blocked,
            _on_recovered=self._release_blocked,
        )

    @final
    async def reload(self, key: str) -> BasePolicyManager[TPolicy]:
        """Refresh a live manager; uncertain writes require ``recover`` instead."""
        return await self._refresh(key, resolver=None)

    @final
    async def recover(
        self,
        key: str,
        resolver: Callable[[str], Awaitable[StoredPolicy[TPolicy] | None]],
    ) -> BasePolicyManager[TPolicy]:
        """Recover after the resolver settles all outstanding writes for ``key``.

        The resolver must confirm the previous operation has finished and
        return authoritative committed state. An ordinary read or a timeout
        alone does not satisfy that contract. Resolver/validation failures keep
        an existing block. Cache failures after installation do not undo recovery.
        """
        return await self._refresh(key, resolver=resolver)

    async def _refresh(
        self,
        key: str,
        *,
        resolver: Callable[[str], Awaitable[StoredPolicy[TPolicy] | None]] | None,
    ) -> BasePolicyManager[TPolicy]:
        from .exceptions import PolicyManagerNotFoundError

        self._check_access(key)
        async with self._lock_for(key):
            manager = self._blocked.get(key) or self._live.get(key)
            if manager is not None and resolver is None:
                manager._ensure_reloadable()
            stored = (
                await self._read_source(key)
                if resolver is None
                else await resolver(key)
            )
            if stored is None:
                error = PolicyManagerNotFoundError(key)
                if manager is not None:
                    manager._mark_unavailable(error, uncertain=False)
                raise error
            if manager is None:
                manager = self._create_manager(key, stored)
                self._live[key] = manager
            else:
                manager._install_loaded(stored)
            await self._write_cache(key, manager)
            return manager

    @abstractmethod
    async def _read_cache(self, key: str) -> BasePolicyManager[TPolicy] | None:
        """Return an unexpired manager reference, or None; never copy it."""
        ...

    @abstractmethod
    async def _read_source(self, key: str) -> StoredPolicy[TPolicy] | None:
        """Load from the same authority used by the injected write store."""
        ...

    @abstractmethod
    async def _write_cache(
        self, key: str, manager: BasePolicyManager[TPolicy]
    ) -> None:
        """Cache this exact object with an accessibility TTL of ``self._ttl``."""
        ...
