"""Infrastructure-independent loading and building contracts."""

from collections.abc import Mapping
from typing import Protocol

from .base import BasePolicy


class PolicyLoader(Protocol):
    """Asynchronously read one complete, internally consistent configuration.

    Use native async I/O, or offload blocking I/O in the adapter. Propagate
    cancellation so a cancelled load cannot proceed to policy publication.
    """

    async def load(self) -> Mapping[str, object]: ...


class PolicyBuilder[TPolicy: BasePolicy](Protocol):
    """Validate and build a domain policy, usually PolicyType.from_config."""

    def __call__(self, raw: Mapping[str, object], /) -> TPolicy: ...
