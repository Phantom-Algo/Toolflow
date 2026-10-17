"""Async source loading followed by synchronous conditional publication."""

from collections.abc import Mapping

from pydantic import ValidationError

from .base import BasePolicy
from .exceptions import PolicyError, PolicyLoadError, PolicyValidationError
from .manager import PolicyManager
from .ports import PolicyBuilder, PolicyLoader
from .types import PolicySnapshot


async def reload_policy[TPolicy: BasePolicy](
    manager: PolicyManager[TPolicy],
    loader: PolicyLoader,
    builder: PolicyBuilder[TPolicy],
) -> PolicySnapshot[TPolicy]:
    """Await a load, then publish against the snapshot taken before loading.

    An explicit reload replaces in-memory values with source values. It does
    not establish remote version order or merge unsaved local changes.

    Cancellation while awaiting the loader propagates without publication.
    Building and publication run synchronously, with no further suspension
    point or background publication task; keep builders and validators short.
    """
    base = manager.snapshot()
    try:
        raw = await loader.load()
    except PolicyError:
        raise
    except Exception as exc:
        raise PolicyLoadError("Could not load policy configuration") from exc
    if not isinstance(raw, Mapping):
        raise PolicyLoadError("Policy loader must return a mapping")
    try:
        candidate = builder(raw)
    except ValidationError as exc:
        raise PolicyValidationError(
            str(exc), errors=exc.errors(include_url=False)
        ) from exc
    return manager.replace(candidate, expected=base)
