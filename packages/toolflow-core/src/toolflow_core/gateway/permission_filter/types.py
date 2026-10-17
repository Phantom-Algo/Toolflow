from dataclasses import dataclass
from typing import Literal

from ...tools import ToolCall

Effect = Literal["allow", "require_approval", "deny"]
Scope = Literal["session", "agent", "application"]
DecisionSource = Literal["tool_effect", "default_effect", "inherit"]


@dataclass(frozen=True)
class ScopeDecision:
    """One scope's contribution to a tool permission decision.

    ``effect`` is ``None`` when the scope does not override its parent and the
    resolution continues. It is never ``None`` for the final decision.
    """

    scope: Scope
    effect: Effect | None
    source: DecisionSource


@dataclass(frozen=True)
class PermissionFilterResult:

    tool_call: ToolCall
    result: Effect
    decisions: tuple[ScopeDecision, ...] = ()

    def effect_for(self, scope: Scope) -> Effect | None:
        for decision in self.decisions:
            if decision.scope == scope:
                return decision.effect
        return None

    @property
    def session_scope_permission(self) -> Effect | None:
        return self.effect_for("session")

    @property
    def agent_scope_permission(self) -> Effect | None:
        return self.effect_for("agent")

    @property
    def application_scope_permission(self) -> Effect | None:
        return self.effect_for("application")
