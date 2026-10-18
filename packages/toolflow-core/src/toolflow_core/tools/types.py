from pydantic import BaseModel, Field
from typing import Any

class ToolCall(BaseModel):

    id: str = Field(
        ..., 
        alias="tool_call_id", 
        description="tool_call_id, tool_call's unique index"
    )

    name: str = Field(
        ...,
        alias="tool_name",
        description="tool_name, tool's unique index"
    )

    arguments: dict[str, Any] = Field(
        ...,
        description="tool_call's arguments which are going to be used in function calling"
    )

    user_id: str = Field(
        ...,
        description="user_id, user's unique index"
    )

    agent_id: str | None = Field(
        None,
        description="agent_id, agent's unique index"
    )

    session_id: str | None = Field(
        None,
        description="session_id, session's unique index"
    )

    metadata: dict[str, Any] | None = Field(
        None,
        description="metadata for current tool_call"
    )

