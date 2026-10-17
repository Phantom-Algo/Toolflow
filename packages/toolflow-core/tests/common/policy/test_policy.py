"""Domain validation, immutable values, and deterministic publication races."""

import asyncio
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import MappingProxyType
from typing import Any, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from toolflow_core.common.policy import (
    BasePolicy,
    PolicyConflictError,
    PolicyImmutabilityError,
    PolicyLoadError,
    PolicyManager,
    PolicySnapshot,
    PolicyValidationError,
    reload_policy,
)


class Limits(BasePolicy):
    low: int = Field(default=1, ge=0)
    high: int = Field(default=10, ge=0)

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        if self.high < self.low:
            raise ValueError("high must be >= low")
        return self


class ExamplePolicy(BasePolicy):
    timeout_ms: int = Field(default=1000, gt=0)
    mode: Literal["fast", "safe"] = "safe"
    limits: Limits = Field(default_factory=Limits)
    scoped: Mapping[str, Limits] = Field(default_factory=dict)
    tools: frozenset[str] = frozenset()
    route: tuple[str, ...] = ()


class PairPolicy(BasePolicy):
    generation: int = 0
    doubled: int = 0

    @model_validator(mode="after")
    def check_pair(self) -> Self:
        if self.doubled != self.generation * 2:
            raise ValueError("mixed generation")
        return self


class MemoryLoader:
    def __init__(self, data: Mapping[str, object]) -> None:
        self.data = data

    async def load(self) -> Mapping[str, object]:
        return self.data


class ForeignModel(BaseModel):
    value: int = 1


def test_deep_freeze_and_input_alias_isolation():
    raw = {
        "scoped": {"agent": {"low": 2, "high": 8}},
        "tools": ["search", "search", "read"],
        "route": ["a", "b"],
    }
    policy = ExamplePolicy.from_config(raw)
    raw["scoped"]["agent"]["low"] = 5
    raw["tools"].append("delete")
    raw["route"].append("c")
    assert policy.scoped["agent"].low == 2
    assert policy.tools == frozenset({"search", "read"})
    assert policy.route == ("a", "b")
    with pytest.raises(TypeError):
        policy.scoped["agent"] = Limits()
    with pytest.raises(ValidationError):
        policy.scoped["agent"].low = 3
    with pytest.raises(ValidationError):
        policy.timeout_ms = 1


def test_direct_construction_and_defaults_are_frozen():
    source = {"a": Limits()}
    policy = ExamplePolicy(scoped=source)
    source.clear()
    assert "a" in policy.scoped
    assert isinstance(ExamplePolicy().scoped, MappingProxyType)


def test_export_round_trip_and_pydantic_serialization():
    policy = ExamplePolicy.from_config(
        {"scoped": {"a": {"low": 2}}, "tools": ["read"], "route": ["a"]}
    )
    exported = policy.to_config()
    restored = ExamplePolicy.from_config(exported)
    assert restored == policy
    assert json.loads(policy.model_dump_json()) == exported
    assert policy.model_dump()["scoped"] == {"a": {"low": 2, "high": 10}}
    assert policy.model_dump(include={"timeout_ms"}) == {"timeout_ms": 1000}
    exported["scoped"]["a"]["low"] = 4
    exported["tools"].append("delete")
    assert policy.scoped["a"].low == 2
    assert policy.tools == frozenset({"read"})


def test_with_changes_replaces_whole_top_level_field():
    old = ExamplePolicy.from_config({"scoped": {"a": {}, "b": {}}})
    new = old.with_changes(scoped={"c": {"low": 3}})
    assert set(new.scoped) == {"c"}
    assert set(old.scoped) == {"a", "b"}
    with pytest.raises(PolicyValidationError):
        old.with_changes(missing=True)
    with pytest.raises(PolicyValidationError):
        Limits().with_changes(low=11)


def test_validation_details_and_exception_chain():
    with pytest.raises(PolicyValidationError) as caught:
        ExamplePolicy.from_config({"timeout_ms": 0})
    assert caught.value.errors()[0]["loc"] == ("timeout_ms",)
    assert isinstance(caught.value.__cause__, ValidationError)
    with pytest.raises(PolicyValidationError):
        ExamplePolicy.from_config({"unknown": 1})
    with pytest.raises(PolicyValidationError):
        ExamplePolicy.from_config([])


@pytest.mark.parametrize("annotation", [list[str], dict[str, int], set[str], Any, object, Mapping[int, str], tuple])
def test_mutable_and_unknown_annotations_rejected_early(annotation):
    with pytest.raises(PolicyImmutabilityError):
        type("InvalidPolicy", (BasePolicy,), {"__annotations__": {"data": annotation}})


def test_nested_any_annotation_rejected():
    with pytest.raises(PolicyImmutabilityError):
        class InvalidPolicy(BasePolicy):
            data: Mapping[str, tuple[Any, ...]]


def test_unsafe_subclass_configuration_and_private_state_rejected():
    with pytest.raises(PolicyImmutabilityError):
        class MutablePolicy(BasePolicy):
            model_config = ConfigDict(frozen=False)

    with pytest.raises(PolicyImmutabilityError):
        class PrivatePolicy(BasePolicy):
            _cache: dict = PrivateAttr(default_factory=dict)


@pytest.mark.parametrize("invalid", [object(), float("nan"), float("inf"), ForeignModel()])
def test_unsupported_raw_values(invalid):
    with pytest.raises(PolicyImmutabilityError):
        ExamplePolicy.from_config({"timeout_ms": invalid})


def test_cycles_rejected_without_rejecting_shared_acyclic_values():
    raw = {}
    raw["scoped"] = raw
    with pytest.raises(PolicyImmutabilityError, match="cyclic"):
        ExamplePolicy.from_config(raw)
    shared = {"low": 2}
    policy = ExamplePolicy.from_config({"scoped": {"a": shared, "b": shared}})
    assert policy.scoped["a"] == policy.scoped["b"]


def test_mapping_proxy_source_is_detached():
    source = {"a": {"low": 2}}
    policy = ExamplePolicy.from_config({"scoped": MappingProxyType(source)})
    source["a"]["low"] = 7
    assert policy.scoped["a"].low == 2
    with pytest.raises(PolicyImmutabilityError, match="keys"):
        ExamplePolicy.from_config({"scoped": {1: {}}})


def test_nested_mapping_tuple_optional_and_fixed_tuple():
    class TreePolicy(BasePolicy):
        data: Mapping[str, tuple[Mapping[str, float | None], ...]]
        pair: tuple[int, str]

    policy = TreePolicy.from_config({"data": {"a": [{"b": None, "c": 1.5}]}, "pair": [1, "x"]})
    with pytest.raises(TypeError):
        policy.data["a"][0]["b"] = 2
    assert TreePolicy.from_config(policy.to_config()) == policy
    assert json.loads(policy.model_dump_json()) == policy.to_config()


def test_aliases_use_canonical_names_for_export_and_update():
    class AliasedPolicy(BasePolicy):
        timeout: int = Field(alias="timeout_ms", gt=0)

    policy = AliasedPolicy.from_config({"timeout_ms": 1})
    assert policy.to_config() == {"timeout": 1}
    assert policy.model_dump(by_alias=True) == {"timeout_ms": 1}
    assert policy.with_changes(timeout=2).timeout == 2
    assert AliasedPolicy.from_config(policy.to_config()) == policy


def test_frozensets_of_nested_policies_and_tuples_round_trip():
    class GroupPolicy(BasePolicy):
        limits: frozenset[Limits]
        pairs: frozenset[tuple[int, str]]

    policy = GroupPolicy.from_config(
        {"limits": frozenset({Limits(low=2)}), "pairs": {(1, "a"), (2, "b")}}
    )
    assert GroupPolicy.from_config(policy.to_config()) == policy
    assert GroupPolicy.from_config(json.loads(policy.model_dump_json())) == policy


def test_forward_references_are_checked_after_model_rebuild():
    class NodePolicy(BasePolicy):
        child: "NodePolicy | None" = None

    policy = NodePolicy.from_config({"child": {"child": None}})
    assert isinstance(policy.child, NodePolicy)
    assert NodePolicy.from_config(policy.to_config()) == policy


def test_initialization_and_publication_isolate_and_revalidate_candidates():
    candidate = ExamplePolicy()
    manager = PolicyManager(candidate)
    base = manager.snapshot()
    assert base.policy is not candidate
    published = manager.replace(candidate, expected=base)
    assert published.policy is not candidate
    assert published.revision == 1
    assert base.revision == 0
    assert manager.snapshot() is published


@pytest.mark.parametrize(
    "candidate",
    [
        ExamplePolicy.model_construct(timeout_ms=0),
        ExamplePolicy().model_copy(update={"timeout_ms": 0}),
        ExamplePolicy().model_copy(update={"unknown": 1}),
        ExamplePolicy.model_construct(),
    ],
)
def test_unvalidated_candidates_cannot_bypass_publication_checks(candidate):
    # model_construct() with defaults only is a valid candidate: make it incomplete.
    if candidate.timeout_ms == 1000 and "unknown" not in candidate.__dict__:
        candidate.__dict__.pop("timeout_ms")
    with pytest.raises(PolicyValidationError):
        PolicyManager(candidate)
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    with pytest.raises(PolicyValidationError):
        manager.replace(candidate, expected=base)
    assert manager.snapshot() is base


def test_nested_unvalidated_model_cannot_bypass_checks():
    candidate = ExamplePolicy.model_construct(
        scoped={"a": Limits.model_construct(low=100, high=1)}
    )
    with pytest.raises(PolicyValidationError):
        PolicyManager(candidate)


def test_wrong_domain_type_rejected():
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    with pytest.raises(PolicyValidationError):
        manager.replace(Limits(), expected=base)
    with pytest.raises(PolicyValidationError):
        PolicyManager({})
    assert manager.snapshot() is base


def test_old_snapshot_is_stable_and_same_value_publish_advances_revision():
    manager = PolicyManager(ExamplePolicy())
    old = manager.snapshot()
    current = manager.update(lambda p: p.with_changes(timeout_ms=2), expected=old)
    assert old.policy.timeout_ms == 1000
    assert current.policy.timeout_ms == 2
    same = manager.replace(current.policy, expected=current)
    assert same.revision == 2
    assert same is not current
    assert same.policy == current.policy


def test_foreign_forged_and_stale_snapshots_rejected():
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    foreign = PolicyManager(ExamplePolicy()).snapshot()
    forged = PolicySnapshot(revision=0, policy=base.policy)
    for expected in (foreign, forged):
        with pytest.raises(PolicyConflictError):
            manager.replace(base.policy, expected=expected)
    published = manager.replace(base.policy, expected=base)
    with pytest.raises(PolicyConflictError) as caught:
        manager.replace(base.policy, expected=base)
    assert caught.value.current_revision == 1
    assert manager.snapshot() is published


def test_transform_failure_preserves_state_and_stale_transform_not_called():
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    calls = []

    def fail(policy):
        calls.append(policy)
        raise RuntimeError("business failure")

    with pytest.raises(RuntimeError, match="business failure"):
        manager.update(fail, expected=base)
    assert manager.snapshot() is base
    manager.replace(base.policy, expected=base)
    with pytest.raises(PolicyConflictError):
        manager.update(fail, expected=base)
    assert len(calls) == 1


def test_two_writers_using_one_base_have_exactly_one_winner():
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    barrier = Barrier(2, timeout=5)

    def write(timeout):
        def transform(policy):
            barrier.wait()
            return policy.with_changes(timeout_ms=timeout)

        try:
            return manager.update(transform, expected=base)
        except PolicyConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(write, 1), pool.submit(write, 2)
        results = [a.result(timeout=5), b.result(timeout=5)]
    assert sum(result is not None for result in results) == 1
    assert manager.snapshot().revision == 1
    assert manager.snapshot().policy.timeout_ms in (1, 2)


def test_readers_always_observe_complete_versions():
    manager = PolicyManager(PairPolicy())
    phase = Barrier(4, timeout=5)

    def read():
        retained = []
        for _ in range(50):
            phase.wait()
            snapshot = manager.snapshot()
            assert snapshot.policy.doubled == 2 * snapshot.policy.generation
            retained.append(snapshot)
            phase.wait()
        for snapshot in retained:
            assert snapshot.policy.generation == snapshot.revision

    with ThreadPoolExecutor(max_workers=3) as pool:
        readers = [pool.submit(read) for _ in range(3)]
        for generation in range(1, 51):
            phase.wait()
            manager.replace(
                PairPolicy(generation=generation, doubled=generation * 2),
                expected=manager.snapshot(),
            )
            phase.wait()
        for reader in readers:
            reader.result(timeout=5)
    assert manager.snapshot().revision == 50


def test_slow_transform_does_not_block_reads_or_retry_after_conflict():
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    entered, release = Event(), Event()
    calls = []

    def slow(policy):
        calls.append(policy)
        entered.set()
        assert release.wait(timeout=5)
        return policy.with_changes(timeout_ms=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(manager.update, slow, expected=base)
        try:
            assert entered.wait(timeout=5)
            assert pool.submit(manager.snapshot).result(timeout=2) is base
            winner = manager.replace(base.policy.with_changes(timeout_ms=3), expected=base)
        finally:
            release.set()
        with pytest.raises(PolicyConflictError):
            writer.result(timeout=5)
    assert len(calls) == 1
    assert manager.snapshot() is winner


def test_publication_validation_runs_outside_the_read_lock():
    enabled, entered, release = Event(), Event(), Event()

    class SlowPolicy(BasePolicy):
        count: int = 1

        @model_validator(mode="after")
        def slow_validation(self) -> Self:
            if enabled.is_set():
                entered.set()
                assert release.wait(timeout=5)
            return self

    candidate = SlowPolicy(count=2)
    manager = PolicyManager(SlowPolicy())
    base = manager.snapshot()
    enabled.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(manager.replace, candidate, expected=base)
        try:
            assert entered.wait(timeout=5)
            assert pool.submit(manager.snapshot).result(timeout=2) is base
        finally:
            release.set()
        result = writer.result(timeout=5)
    assert result.policy.count == 2
    assert result.revision == 1


def test_json_file_loader_example(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"timeout_ms": 3}', encoding="utf-8")

    class JsonLoader:
        async def load(self):
            return json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))

    manager = PolicyManager(ExamplePolicy())
    result = asyncio.run(reload_policy(manager, JsonLoader(), ExamplePolicy.from_config))
    assert result.revision == 1
    assert result.policy.timeout_ms == 3


@pytest.mark.parametrize("slow_stage", ["load", "build"])
def test_slow_reload_does_not_block_readers_or_overwrite_new_policy(slow_stage):
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    entered, release = Event(), Event()

    def pause():
        entered.set()
        assert release.wait(timeout=5)

    class SlowLoader:
        async def load(self):
            if slow_stage == "load":
                await asyncio.to_thread(pause)
            return {"timeout_ms": 2}

    def builder(raw):
        if slow_stage == "build":
            pause()
        return ExamplePolicy.from_config(raw)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reload = pool.submit(asyncio.run, reload_policy(manager, SlowLoader(), builder))
        try:
            assert entered.wait(timeout=5)
            assert pool.submit(manager.snapshot).result(timeout=2) is base
            winner = manager.replace(base.policy.with_changes(timeout_ms=3), expected=base)
        finally:
            release.set()
        with pytest.raises(PolicyConflictError):
            reload.result(timeout=5)
    assert manager.snapshot() is winner


def test_load_errors_and_validation_errors_preserve_current_snapshot():
    class BrokenLoader:
        async def load(self):
            raise OSError("source offline")

    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    with pytest.raises(PolicyLoadError) as caught:
        asyncio.run(reload_policy(manager, BrokenLoader(), ExamplePolicy.from_config))
    assert isinstance(caught.value.__cause__, OSError)
    with pytest.raises(PolicyValidationError):
        asyncio.run(
            reload_policy(manager, MemoryLoader({"timeout_ms": 0}), ExamplePolicy.from_config)
        )
    with pytest.raises(PolicyLoadError):
        asyncio.run(reload_policy(manager, MemoryLoader([]), ExamplePolicy.from_config))
    assert manager.snapshot() is base


def test_custom_builder_pydantic_error_is_normalized():
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    with pytest.raises(PolicyValidationError) as caught:
        asyncio.run(
            reload_policy(manager, MemoryLoader({"timeout_ms": 0}), ExamplePolicy.model_validate)
        )
    assert caught.value.errors()[0]["loc"] == ("timeout_ms",)
    assert isinstance(caught.value.__cause__, ValidationError)
    assert manager.snapshot() is base


def test_explicit_reload_can_replace_unsaved_local_changes():
    manager = PolicyManager(ExamplePolicy())
    base = manager.snapshot()
    manager.update(lambda p: p.with_changes(timeout_ms=2), expected=base)
    result = asyncio.run(
        reload_policy(manager, MemoryLoader({"timeout_ms": 1}), ExamplePolicy.from_config)
    )
    assert result.revision == 2
    assert result.policy.timeout_ms == 1
