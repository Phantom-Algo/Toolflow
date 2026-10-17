"""Static checks for the public generic contracts; checked with mypy."""

from typing import assert_type

from toolflow_core.common.policy import (
    BasePolicy,
    PolicyBuilder,
    PolicyLoader,
    PolicyManager,
    PolicySnapshot,
    reload_policy,
)


class RuntimePolicy(BasePolicy):
    timeout_ms: int = 3000


async def check_contracts(loader: PolicyLoader, initial: RuntimePolicy) -> None:
    manager = PolicyManager(initial)
    snapshot = manager.snapshot()
    assert_type(snapshot, PolicySnapshot[RuntimePolicy])
    assert_type(snapshot.policy, RuntimePolicy)
    builder: PolicyBuilder[RuntimePolicy] = RuntimePolicy.from_config
    published = await reload_policy(manager, loader, builder)
    assert_type(published, PolicySnapshot[RuntimePolicy])
    assert_type(
        manager.update(lambda policy: policy.with_changes(timeout_ms=1), expected=published),
        PolicySnapshot[RuntimePolicy],
    )
