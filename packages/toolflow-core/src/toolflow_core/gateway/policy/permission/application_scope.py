from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from .base import BaseToolPermissionPolicy


class ApplicationScopeToolPermissionPolicy(BaseToolPermissionPolicy):
    """
    Application defaults for tool permission decisions.

    For each tool, use its entry in tool_effects when present; otherwise use
    default_effect. Keys are unique tool identifiers.

    Effects mean direct execution ("allow"), human approval required before
    execution ("require_approval"), or execution prohibited ("deny"). Approval
    requests and their runtime status are not stored in this policy.

    These are overridable defaults, not an application-wide permission ceiling.
    Child scopes may replace any effect, including "deny" and
    "require_approval". Effects are not combined by severity across scopes.
    """

    default_effect: Literal["allow", "require_approval", "deny"] = Field(
        default="allow",
        description="Application permission effect for tools without an explicit entry.",
    )

    tool_effects: Mapping[str, Literal["allow", "require_approval", "deny"]] = Field(
        default_factory=dict,
        description="Permission effect by unique tool identifier; overrides default_effect.",
    )
