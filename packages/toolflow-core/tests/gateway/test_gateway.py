"""Gateway integration with scope-specific repositories and durable updates."""

import asyncio
import json

import pytest

from policy_test_support import MemoryPolicyStore, checkpoint, run_async
from toolflow_core.common.policy import (
    CachetoolsPolicyManagerRepo,
    PolicyManagerNotFoundError,
    PolicyUnavailableError,
    PolicyWriteUncertainError,
)
from toolflow_core.gateway import PermissionManagerKeys, ToolflowGateway
from toolflow_core.gateway.policy.permission import (
    AgentScopeToolPermissionPolicy as AgentPolicy,
    ApplicationScopeToolPermissionPolicy as ApplicationPolicy,
    SessionScopeToolPermissionPolicy as SessionPolicy,
)
from toolflow_core.tools import ToolCall


def key(*parts):
    return json.dumps(parts)


def make_call(session="session-A", agent="agent-1"):
    return ToolCall(
        tool_call_id=f"call-{session}", tool_name="search", arguments={},
        user_id="user-1", session_id=session, agent_id=agent,
    )


def setup_gateway():
    session_store = MemoryPolicyStore({
        key("user-1", "session-A"): SessionPolicy(policy_id="session-A"),
        key("user-1", "session-B"): SessionPolicy(policy_id="session-B"),
    })
    agent_store = MemoryPolicyStore({
        key("user-1", "agent-1"): AgentPolicy(
            policy_id="agent-1", tool_effects={"search": "allow"}
        ),
    })
    application_store = MemoryPolicyStore({
        key("user-1"): ApplicationPolicy(policy_id="user-1", default_effect="deny"),
    })
    repos = tuple(CachetoolsPolicyManagerRepo(s) for s in (
        session_store, agent_store, application_store
    ))
    gateway = ToolflowGateway(*repos, resolve_keys=lambda call: PermissionManagerKeys(
        session=key(call.user_id, call.session_id),
        agent=key(call.user_id, call.agent_id),
        application=key(call.user_id),
    ))
    return gateway, repos, (session_store, agent_store, application_store)


@run_async
async def test_sessions_share_updated_agent_and_user_application_managers():
    gateway, repos, stores = setup_gateway()
    calls = (make_call("session-A"), make_call("session-B"))
    first = await asyncio.gather(*(gateway.prepare(c) for c in calls))
    assert all(result.result == "allow" for result in first)
    assert len(stores[0].loads) == 2 and len(stores[1].loads) == len(stores[2].loads) == 1
    manager = await repos[1].get(key("user-1", "agent-1"))
    base = await manager.snapshot()
    await manager.replace(base.policy.with_changes(tool_effects={"search": "deny"}), expected=base)
    second = await asyncio.gather(*(gateway.prepare(c) for c in calls))
    assert all(result.result == "deny" for result in second)
    assert await repos[1].get(key("user-1", "agent-1")) is manager
    assert len(stores[1].loads) == 1


@run_async
async def test_prepare_waits_for_agent_commit_then_checks_new_permissions():
    gateway, repos, stores = setup_gateway()
    manager = await repos[1].get(key("user-1", "agent-1"))
    base = await manager.snapshot()
    stores[1].save_release = asyncio.Event()
    writer = asyncio.create_task(manager.replace(
        base.policy.with_changes(tool_effects={"search": "deny"}), expected=base
    ))
    await stores[1].save_started.wait()
    pending = asyncio.create_task(gateway.prepare(make_call()))
    await checkpoint()
    assert not pending.done()
    stores[1].save_release.set()
    await writer
    assert (await pending).result == "deny"


@run_async
async def test_source_failure_stops_acquisition_before_later_scopes():
    gateway, _, stores = setup_gateway()
    stores[0].load_error = OSError("database unavailable")
    with pytest.raises(OSError):
        await gateway.prepare(make_call())
    assert not stores[1].loads and not stores[2].loads


@run_async
async def test_missing_agent_is_not_treated_as_implicit_inheritance():
    gateway, _, stores = setup_gateway()
    with pytest.raises(PolicyManagerNotFoundError):
        await gateway.prepare(make_call(agent="unconfigured"))
    assert not stores[2].loads


@run_async
async def test_uncertain_agent_commit_blocks_future_permission_resolution():
    gateway, repos, stores = setup_gateway()
    manager = await repos[1].get(key("user-1", "agent-1"))
    base = await manager.snapshot()
    stores[1].save_error = PolicyWriteUncertainError("outcome unknown")
    with pytest.raises(PolicyWriteUncertainError):
        await manager.replace(base.policy.with_changes(default_effect="deny"), expected=base)
    with pytest.raises(PolicyUnavailableError):
        await gateway.prepare(make_call())
    assert not stores[2].loads
