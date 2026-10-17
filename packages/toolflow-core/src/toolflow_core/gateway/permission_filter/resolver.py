from collections.abc import Iterator

from ...common.policy import PolicyManager
from ...tools import ToolCall
from ..policy.permission import (
    ApplicationScopeToolPermissionPolicy,
    AgentScopeToolPermissionPolicy,
    SessionScopeToolPermissionPolicy,
)
from .rules import decide_agent, decide_application, decide_session
from .types import PermissionFilterResult, ScopeDecision


class PermissionResolver:
    """Resolves a tool call from the most specific scope to the least specific.

    Each scope records one decision. The first non-``None`` effect is final, so
    less specific scopes are not consulted and keep ``None`` in the result.
    """

    def __init__(
        self,
        session_manager: PolicyManager[SessionScopeToolPermissionPolicy],
        agent_manager: PolicyManager[AgentScopeToolPermissionPolicy],
        application_manager: PolicyManager[ApplicationScopeToolPermissionPolicy],
    ) -> None:
        self._session_manager = session_manager
        self._agent_manager = agent_manager
        self._application_manager = application_manager

    def resolve(self, tool_call: ToolCall) -> PermissionFilterResult:
        decisions: list[ScopeDecision] = []

        for decision in self._decisions(tool_call):
            decisions.append(decision)
            if decision.effect is not None:
                return PermissionFilterResult(
                    tool_call=tool_call,
                    result=decision.effect,
                    decisions=tuple(decisions),
                )

        raise RuntimeError("no permission scope produced a decision")

    def _decisions(self, tool_call: ToolCall) -> Iterator[ScopeDecision]:
        yield decide_session(tool_call, self._session_manager.snapshot().policy)
        yield decide_agent(tool_call, self._agent_manager.snapshot().policy)
        yield decide_application(tool_call, self._application_manager.snapshot().policy)
