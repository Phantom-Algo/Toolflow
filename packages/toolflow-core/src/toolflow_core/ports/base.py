from typing import Protocol, TypeVar
from ..tools import ToolCall

TRawToolCall = TypeVar("TRawToolCall")

class ToolCallAdapter(Protocol[TRawToolCall]):

    def to_tool_call(raw: TRawToolCall) -> ToolCall:
        ...