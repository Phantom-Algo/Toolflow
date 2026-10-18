"""Static checks for the public asynchronous generic contracts; checked with mypy."""

from typing import assert_type

from toolflow_core.common.policy import (
    BasePolicy,
    BasePolicyManager,
    BasePolicyManagerRepo,
    CachetoolsPolicyManagerRepo,
    PolicyManager,
    PolicySnapshot,
    PolicyStore,
    StoredPolicy,
)


class RuntimePolicy(BasePolicy):
    timeout_ms: int = 3000


async def check_contracts(initial: RuntimePolicy, store: PolicyStore[RuntimePolicy]) -> None:
    manager: BasePolicyManager[RuntimePolicy] = PolicyManager(
        StoredPolicy(initial, "initial-version"), key="agent", store=store
    )
    snapshot = await manager.snapshot()
    assert_type(snapshot, PolicySnapshot[RuntimePolicy])
    assert_type(snapshot.policy, RuntimePolicy)
    assert_type(snapshot.source_version, str)
    published = await manager.update(
        lambda policy: policy.with_changes(timeout_ms=1), expected=snapshot
    )
    assert_type(published, PolicySnapshot[RuntimePolicy])
    assert_type(
        await manager.replace(published.policy, expected=published),
        PolicySnapshot[RuntimePolicy],
    )
    repo: BasePolicyManagerRepo[RuntimePolicy] = CachetoolsPolicyManagerRepo(store)
    assert_type(await repo.get("agent"), BasePolicyManager[RuntimePolicy])
    assert_type(await repo.reload("agent"), BasePolicyManager[RuntimePolicy])
    assert_type(await repo.recover("agent", store.load), BasePolicyManager[RuntimePolicy])
