from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from .base import BaseToolPermissionPolicy


class AgentScopeToolPermissionPolicy(BaseToolPermissionPolicy):
    """
    Agent overrides of the parent Application scope's tool permissions.

    For each tool, use its entry in tool_effects when present. Otherwise, use
    the parent's resolved effect when default_effect is "inherit", or use the
    Agent's default_effect. A concrete default replaces the parent's entire
    decision for unlisted tools, including explicit parent tool entries.

    Effects mean direct execution ("allow"), human approval required before
    execution ("require_approval"), or execution prohibited ("deny"). "inherit"
    selects the parent decision; it is not a final permission effect.

    Any local effect may replace any parent effect, including "deny" and
    "require_approval". Effects are not combined by severity across scopes.
    Keys are unique tool identifiers. Approval requests and their runtime status
    are not stored in this policy.
    """

    default_effect: Literal["inherit", "allow", "require_approval", "deny"] = Field(
        default="deny",
        description=(
            "Effect for tools without a local entry; 'inherit' uses the parent's "
            "resolved effect, while a concrete effect replaces it."
        ),
    )

    tool_effects: Mapping[str, Literal["allow", "require_approval", "deny"]] = Field(
        default_factory=dict,
        description="Permission effect by unique tool identifier; overrides default_effect and the parent.",
    )
