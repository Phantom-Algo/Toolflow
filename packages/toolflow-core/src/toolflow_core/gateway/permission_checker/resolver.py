from collections.abc import AsyncIterator

from ...common.policy import BasePolicyManager
from ...tools import ToolCall
from ..policy.permission import (
    ApplicationScopeToolPermissionPolicy,
    AgentScopeToolPermissionPolicy,
    SessionScopeToolPermissionPolicy,
)
from .rules import decide_agent, decide_application, decide_session
from .types import PermissionCheckerResult, ScopeDecision


class PermissionResolver:
    """Resolves a tool call from the most specific scope to the least specific.

    Each scope records one decision. The first non-``None`` effect is final, so
    less specific scopes are not consulted and keep ``None`` in the result.
    """

    def __init__(
        self,
        session_manager: BasePolicyManager[SessionScopeToolPermissionPolicy],
        agent_manager: BasePolicyManager[AgentScopeToolPermissionPolicy],
        application_manager: BasePolicyManager[ApplicationScopeToolPermissionPolicy],
    ) -> None:
        self._session_manager = session_manager
        self._agent_manager = agent_manager
        self._application_manager = application_manager

    async def resolve(self, tool_call: ToolCall) -> PermissionCheckerResult:
        decisions: list[ScopeDecision] = []

        async for decision in self._decisions(tool_call):
            decisions.append(decision)
            if decision.effect is not None:
                return PermissionCheckerResult(
                    tool_call=tool_call,
                    result=decision.effect,
                    decisions=tuple(decisions),
                )

        raise RuntimeError("no permission scope produced a decision")

    async def _decisions(self, tool_call: ToolCall) -> AsyncIterator[ScopeDecision]:
        yield decide_session(tool_call, (await self._session_manager.snapshot()).policy)
        yield decide_agent(tool_call, (await self._agent_manager.snapshot()).policy)
        yield decide_application(tool_call, (await self._application_manager.snapshot()).policy)
