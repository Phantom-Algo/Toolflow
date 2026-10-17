from pydantic import Field

from ....common.policy import BasePolicy

class BaseToolPermissionPolicy(BasePolicy):

    policy_id: str = Field(
        ...,
        description="policy_id, policy's unique index"
    )