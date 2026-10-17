from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from .base import BaseToolPermissionPolicy


class SessionScopeToolPermissionPolicy(BaseToolPermissionPolicy):
    """
    Session overrides of the parent Agent scope's tool permissions.

    For each tool, use its entry in tool_effects when present; otherwise inherit
    the parent's resolved effect. Sessions have no configurable default effect.

    Effects mean direct execution ("allow"), human approval required before
    execution ("require_approval"), or execution prohibited ("deny"). Any local
    effect may replace any inherited effect, including "deny" and
    "require_approval". Effects are not combined by severity across scopes.

    Keys are unique tool identifiers. An "allow" entry is a session policy
    override, not a record that a particular invocation was approved. Approval
    requests and their runtime status are not stored in this policy.
    """

    tool_effects: Mapping[str, Literal["allow", "require_approval", "deny"]] = Field(
        default_factory=dict,
        description="Permission effect by unique tool identifier; overrides the inherited Agent scope effect.",
    )
