"""Acquire shared scope managers for each tool call before permission resolution."""

from collections.abc import Callable
from dataclasses import dataclass

from ..common.policy import BasePolicyManagerRepo
from ..tools import ToolCall
from .base import BaseToolflowGateway
from .permission_checker import PermissionCheckerResult, PermissionResolver
from .policy.permission import (
    AgentScopeToolPermissionPolicy,
    ApplicationScopeToolPermissionPolicy,
    SessionScopeToolPermissionPolicy,
)


@dataclass(frozen=True, slots=True)
class PermissionManagerKeys:
    """Trusted application mapping from a tool call to three persisted policies.

    Agent keys must be independent of session identity. Application keys belong
    to the user. Resolve missing optional scopes to explicit inheritance-policy
    records; a missing source record is an acquisition error, not permission.
    """

    session: str
    agent: str
    application: str


class ToolflowGateway(BaseToolflowGateway):
    """Reuse injected repos; construct a lightweight resolver per tool call."""

    def __init__(
        self,
        session_repo: BasePolicyManagerRepo[SessionScopeToolPermissionPolicy],
        agent_repo: BasePolicyManagerRepo[AgentScopeToolPermissionPolicy],
        application_repo: BasePolicyManagerRepo[ApplicationScopeToolPermissionPolicy],
        *,
        resolve_keys: Callable[[ToolCall], PermissionManagerKeys],
    ) -> None:
        self._session_repo = session_repo
        self._agent_repo = agent_repo
        self._application_repo = application_repo
        self._resolve_keys = resolve_keys

    # TODO：这里的 PermissionCheckerResult 只是当前版本的返回结果，不是最终版本的，后续需要替换掉
    async def prepare(self, tool_call: ToolCall) -> PermissionCheckerResult:
        # Step 1: fetch managers
        keys = self._resolve_keys(tool_call)
        session_policy_manager = await self._session_repo.get(keys.session)
        agent_policy_manager = await self._agent_repo.get(keys.agent)
        application_policy_manager = await self._application_repo.get(keys.application)

        # Step 2: check tool's permission
        return await PermissionResolver(session_policy_manager, agent_policy_manager, application_policy_manager).resolve(tool_call)
