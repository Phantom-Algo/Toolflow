from abc import ABC, abstractmethod

from ..tools import ToolCall
from .permission_checker import PermissionCheckerResult

class BaseToolflowGateway(ABC):
    """Asynchronous permission preparation before a tool reaches execution."""

    @abstractmethod
    async def prepare(self, tool_call: ToolCall) -> PermissionCheckerResult:
        """Resolve permissions, propagating policy acquisition failures."""
        ...
