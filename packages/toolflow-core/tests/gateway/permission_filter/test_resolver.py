"""Permission resolution: scope precedence and accumulated decisions."""

from policy_test_support import make_manager, run_async
from toolflow_core.common.policy import BasePolicy
from toolflow_core.gateway.permission_checker import (
    PermissionCheckerResult,
    PermissionResolver,
    ScopeDecision,
    decide_agent,
    decide_application,
    decide_session,
)
from toolflow_core.gateway.policy.permission import (
    AgentScopeToolPermissionPolicy,
    ApplicationScopeToolPermissionPolicy,
    SessionScopeToolPermissionPolicy,
)
from toolflow_core.tools import ToolCall


def make_tool_call(name: str = "search") -> ToolCall:
    return ToolCall(
        tool_call_id="call-1",
        tool_name=name,
        arguments={},
        user_id="user-1",
    )


def make_resolver(
    *,
    session: dict[str, object] | None = None,
    agent: dict[str, object] | None = None,
    application: dict[str, object] | None = None,
) -> PermissionResolver:
    return PermissionResolver(
        make_manager(
            SessionScopeToolPermissionPolicy(
                policy_id="session", **(session or {})
            )
        ),
        make_manager(
            AgentScopeToolPermissionPolicy(policy_id="agent", **(agent or {}))
        ),
        make_manager(
            ApplicationScopeToolPermissionPolicy(
                policy_id="application", **(application or {})
            )
        ),
    )


@run_async
async def test_session_effect_short_circuits_more_general_scopes() -> None:
    resolver = make_resolver(
        session={"tool_effects": {"search": "deny"}},
        agent={"tool_effects": {"search": "allow"}},
        application={"tool_effects": {"search": "allow"}},
    )

    result = await resolver.resolve(make_tool_call())

    assert result.result == "deny"
    assert result.session_scope_permission == "deny"
    assert result.agent_scope_permission is None
    assert result.application_scope_permission is None
    assert result.decisions == (
        ScopeDecision(scope="session", effect="deny", source="tool_effect"),
    )


@run_async
async def test_agent_effect_resolves_after_session_inherits() -> None:
    resolver = make_resolver(
        agent={"tool_effects": {"search": "require_approval"}},
    )

    result = await resolver.resolve(make_tool_call())

    assert result.result == "require_approval"
    assert result.session_scope_permission is None
    assert result.agent_scope_permission == "require_approval"
    assert result.application_scope_permission is None
    assert result.decisions == (
        ScopeDecision(scope="session", effect=None, source="inherit"),
        ScopeDecision(
            scope="agent", effect="require_approval", source="tool_effect"
        ),
    )


@run_async
async def test_agent_default_replaces_application_tool_entry() -> None:
    resolver = make_resolver(
        agent={"default_effect": "deny"},
        application={"tool_effects": {"search": "allow"}},
    )

    result = await resolver.resolve(make_tool_call())

    assert result.result == "deny"
    assert result.agent_scope_permission == "deny"
    assert result.application_scope_permission is None


@run_async
async def test_application_tool_entry_resolves_after_both_parents_inherit() -> None:
    resolver = make_resolver(
        agent={"default_effect": "inherit"},
        application={"tool_effects": {"search": "require_approval"}},
    )

    result = await resolver.resolve(make_tool_call())

    assert result.result == "require_approval"
    assert result.session_scope_permission is None
    assert result.agent_scope_permission is None
    assert result.application_scope_permission == "require_approval"


@run_async
async def test_application_default_is_the_final_fallback() -> None:
    resolver = make_resolver(
        agent={"default_effect": "inherit"},
        application={"default_effect": "allow"},
    )

    result = await resolver.resolve(make_tool_call())

    assert result.result == "allow"
    assert result.application_scope_permission == "allow"
    assert result.decisions == (
        ScopeDecision(scope="session", effect=None, source="inherit"),
        ScopeDecision(scope="agent", effect=None, source="inherit"),
        ScopeDecision(
            scope="application", effect="allow", source="default_effect"
        ),
    )


@run_async
async def test_short_circuit_does_not_snapshot_downstream_managers() -> None:
    class SpyManager:
        def __init__(self, policy: BasePolicy) -> None:
            self._manager = make_manager(policy)
            self.calls = 0

        async def snapshot(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            return await self._manager.snapshot()

    agent = SpyManager(AgentScopeToolPermissionPolicy(policy_id="agent"))
    application = SpyManager(
        ApplicationScopeToolPermissionPolicy(policy_id="application")
    )
    resolver = PermissionResolver(
        make_manager(
            SessionScopeToolPermissionPolicy(
                policy_id="session", tool_effects={"search": "deny"}
            )
        ),
        agent,  # type: ignore[arg-type]
        application,  # type: ignore[arg-type]
    )

    await resolver.resolve(make_tool_call())

    assert agent.calls == 0
    assert application.calls == 0


def test_rules_report_inherit_and_default_sources() -> None:
    call = make_tool_call()

    assert decide_session(
        call, SessionScopeToolPermissionPolicy(policy_id="session")
    ) == ScopeDecision(scope="session", effect=None, source="inherit")

    assert decide_agent(
        call, AgentScopeToolPermissionPolicy(policy_id="agent")
    ) == ScopeDecision(scope="agent", effect="deny", source="default_effect")

    assert decide_agent(
        call,
        AgentScopeToolPermissionPolicy(policy_id="agent", default_effect="inherit"),
    ) == ScopeDecision(scope="agent", effect=None, source="inherit")

    assert decide_application(
        call, ApplicationScopeToolPermissionPolicy(policy_id="application")
    ) == ScopeDecision(
        scope="application", effect="allow", source="default_effect"
    )


@run_async
async def test_result_effect_for_returns_only_visited_scopes() -> None:
    resolver = make_resolver(
        session={"tool_effects": {"search": "allow"}},
        agent={"tool_effects": {"search": "deny"}},
    )

    result = await resolver.resolve(make_tool_call())

    assert isinstance(result, PermissionCheckerResult)
    assert result.effect_for("session") == "allow"
    assert result.effect_for("agent") is None
    assert result.effect_for("application") is None


@run_async
async def test_resolver_is_reusable_across_tool_calls() -> None:
    resolver = make_resolver(
        session={"tool_effects": {"search": "deny"}},
        agent={"default_effect": "inherit"},
        application={"default_effect": "allow"},
    )

    denied = await resolver.resolve(make_tool_call("search"))
    allowed = await resolver.resolve(make_tool_call("write"))

    assert denied.result == "deny"
    assert allowed.result == "allow"
    assert denied.decisions != allowed.decisions
