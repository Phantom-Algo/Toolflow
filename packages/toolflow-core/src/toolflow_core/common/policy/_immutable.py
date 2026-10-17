"""Internal helpers for the deliberately restricted policy value tree."""

from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType, UnionType
from typing import Annotated, Literal, Union, get_args, get_origin

from .exceptions import PolicyImmutabilityError, PolicyValidationError


def check_annotation(annotation: object, path: str) -> None:
    """Reject mutable or unbounded field types once a model is complete."""
    from .base import BasePolicy

    if annotation in (str, bool, int, float, type(None)):
        return
    if isinstance(annotation, type) and issubclass(annotation, BasePolicy):
        return
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        check_annotation(args[0], path)
        return
    if origin in (Union, UnionType):
        for member in args:
            check_annotation(member, path)
        return
    if origin is Literal and args:
        for member in args:
            if type(member) not in (str, bool, int, float, type(None)):
                raise PolicyImmutabilityError(f"{path}: unsupported literal {member!r}")
        return
    if origin is Mapping and len(args) == 2 and args[0] is str:
        check_annotation(args[1], f"{path}[*]")
        return
    if origin in (tuple, frozenset) and args:
        for member in args:
            if member is not Ellipsis:
                check_annotation(member, f"{path}[*]")
        return
    raise PolicyImmutabilityError(
        f"{path}: unsupported annotation {annotation!r}; use scalar values, "
        "BasePolicy, tuple, frozenset, or Mapping[str, T]"
    )


def copy_tree(
    value: object,
    *,
    frozen: bool = False,
    export: bool = False,
    path: str = "$",
    active: set[int] | None = None,
) -> object:
    """Copy containers, reject cycles, and optionally freeze or export them.

    Input copying preserves tuples, but normalizes sets to input lists so nested
    policies can become dictionaries. Freezing accepts validated value shapes.
    """
    from .base import BasePolicy

    if type(value) in (str, bool, int, type(None)):
        return value
    if type(value) is float:
        if not isfinite(value):
            raise PolicyImmutabilityError(f"{path}: non-finite floats are unsupported")
        return value

    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise PolicyImmutabilityError(f"{path}: cyclic policy data")
    active.add(identity)
    try:
        def visit(child: object, child_path: str) -> object:
            return copy_tree(
                child, frozen=frozen, export=export, path=child_path, active=active
            )

        if isinstance(value, BasePolicy):
            if frozen:
                # Nested policies have already run their own validation/freeze.
                return value
            fields = type(value).model_fields
            extra = value.__dict__.keys() - fields.keys()
            missing = fields.keys() - value.__dict__.keys()
            if extra or missing or value.__pydantic_extra__:
                raise PolicyValidationError(
                    f"{path}: invalid policy fields (extra={sorted(extra)}, "
                    f"missing={sorted(missing)})"
                )
            return {
                name: visit(value.__dict__[name], f"{path}.{name}")
                for name in fields
            }
        if isinstance(value, Mapping):
            result = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise PolicyImmutabilityError(f"{path}: mapping keys must be strings")
                result[key] = visit(child, f"{path}[{key!r}]")
            return MappingProxyType(result) if frozen else result
        if isinstance(value, (tuple, frozenset, list, set)) and type(value) in (
            tuple, frozenset, list, set
        ):
            if frozen and type(value) in (list, set):
                raise PolicyImmutabilityError(
                    f"{path}: mutable value after validation; declare tuple or frozenset"
                )
            items = [visit(child, f"{path}[{index}]") for index, child in enumerate(value)]
            if export or (not frozen and type(value) in (set, frozenset)):
                return items
            return type(value)(items)
        raise PolicyImmutabilityError(f"{path}: unsupported value type {type(value).__name__}")
    finally:
        active.remove(identity)


def serializer_value(value: object) -> object:
    """Replace mapping proxies without changing Pydantic's other value types."""
    if isinstance(value, Mapping):
        return {key: serializer_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, frozenset)) and type(value) in (tuple, frozenset):
        return type(value)(serializer_value(child) for child in value)
    return value
