"""Run from the repository root with uv run --package toolflow-core <this file>."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import Field

from toolflow_core.common.policy import (
    BasePolicy,
    PolicyConflictError,
    PolicyManager,
    reload_policy,
)


class RuntimePolicy(BasePolicy):
    timeout_ms: int = Field(default=3000, gt=0)
    allowed_tools: frozenset[str] = frozenset()
    tool_timeouts: Mapping[str, int] = Field(default_factory=dict)


@dataclass(frozen=True)
class JsonPolicyLoader:
    path: Path

    async def load(self) -> Mapping[str, object]:
        return await asyncio.to_thread(self._read_file)

    def _read_file(self) -> Mapping[str, object]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("policy JSON must be an object")
        return raw


async def main() -> None:
    with TemporaryDirectory(prefix="toolflow-policy-") as directory:
        path = Path(directory) / "policy.json"
        await asyncio.to_thread(
            path.write_text,
            json.dumps({"timeout_ms": 3000, "allowed_tools": ["search"]}),
            encoding="utf-8",
        )
        loader = JsonPolicyLoader(path)
        manager = PolicyManager(RuntimePolicy.from_config(await loader.load()))
        old = manager.snapshot()

        published = manager.update(
            lambda policy: policy.with_changes(timeout_ms=5000), expected=old
        )
        print(f"Old snapshot: revision={old.revision}, timeout={old.policy.timeout_ms}")
        print(f"Published: revision={published.revision}, timeout={published.policy.timeout_ms}")

        try:
            manager.replace(old.policy, expected=old)
        except PolicyConflictError:
            print("Stale update rejected; published policy is unchanged.")

        # Optional persistence saves this exact publication, outside the manager.
        await asyncio.to_thread(
            path.write_text, published.policy.model_dump_json(), encoding="utf-8"
        )
        reloaded = await reload_policy(manager, loader, RuntimePolicy.from_config)
        print(f"Reloaded: revision={reloaded.revision}, config={reloaded.policy.to_config()}")


if __name__ == "__main__":
    asyncio.run(main())
