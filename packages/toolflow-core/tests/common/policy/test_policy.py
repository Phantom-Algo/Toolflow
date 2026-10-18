"""Domain validation, deep immutability and configuration round trips."""

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from toolflow_core.common.policy import (
    BasePolicy,
    PolicyImmutabilityError,
    PolicyValidationError,
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
