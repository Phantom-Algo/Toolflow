"""Pure per-scope decision rules, ordered from most to least specific."""

from ...tools import ToolCall
from ..policy.permission import (
    ApplicationScopeToolPermissionPolicy,
    AgentScopeToolPermissionPolicy,
    SessionScopeToolPermissionPolicy,
)
from .types import ScopeDecision


def decide_session(
    tool_call: ToolCall,
    policy: SessionScopeToolPermissionPolicy,
) -> ScopeDecision:
    effect = policy.tool_effects.get(tool_call.name)
    if effect is None:
        return ScopeDecision(scope="session", effect=None, source="inherit")
    return ScopeDecision(scope="session", effect=effect, source="tool_effect")


def decide_agent(
    tool_call: ToolCall,
    policy: AgentScopeToolPermissionPolicy,
) -> ScopeDecision:
    effect = policy.tool_effects.get(tool_call.name)
    if effect is not None:
        return ScopeDecision(scope="agent", effect=effect, source="tool_effect")

    if policy.default_effect != "inherit":
        return ScopeDecision(
            scope="agent",
            effect=policy.default_effect,
            source="default_effect",
        )

    return ScopeDecision(scope="agent", effect=None, source="inherit")


def decide_application(
    tool_call: ToolCall,
    policy: ApplicationScopeToolPermissionPolicy,
) -> ScopeDecision:
    effect = policy.tool_effects.get(tool_call.name)
    if effect is not None:
        return ScopeDecision(
            scope="application",
            effect=effect,
            source="tool_effect",
        )

    return ScopeDecision(
        scope="application",
        effect=policy.default_effect,
        source="default_effect",
    )
