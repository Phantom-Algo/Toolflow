"""Validated, deeply immutable domain configuration models."""

from collections.abc import Mapping
from typing import Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_serializer,
    model_validator,
)

from .exceptions import PolicyImmutabilityError, PolicyValidationError


class BasePolicy(BaseModel):

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        validate_default=True,
        allow_inf_nan=False,
    )

    @classmethod
    def __pydantic_on_complete__(cls) -> None:
        super().__pydantic_on_complete__()
        from ._immutable import check_annotation

        required = {
            "frozen": True,
            "extra": "forbid",
            "revalidate_instances": "always",
            "validate_default": True,
            "allow_inf_nan": False,
        }
        if any(cls.model_config.get(key) != value for key, value in required.items()):
            raise PolicyImmutabilityError("Policy subclasses must preserve BasePolicy config")
        if cls.__private_attributes__ or cls.model_computed_fields:
            raise PolicyImmutabilityError("Policies cannot contain private or computed state")
        for name, field in cls.model_fields.items():
            check_annotation(field.annotation, f"{cls.__name__}.{name}")

    @model_validator(mode="after")
    def _freeze_fields(self) -> Self:
        from ._immutable import copy_tree

        for name in type(self).model_fields:
            object.__setattr__(
                self,
                name,
                copy_tree(getattr(self, name), frozen=True, path=name, active={id(self)}),
            )
        return self

    @field_serializer("*", mode="wrap", check_fields=False)
    def _serialize_field(
        self, value: object, handler: SerializerFunctionWrapHandler
    ) -> object:
        from ._immutable import serializer_value

        return handler(serializer_value(value))

    @classmethod
    def from_config(cls, raw: Mapping[str, object]) -> Self:
        """Validate an isolated configuration tree and freeze all containers."""
        from ._immutable import copy_tree

        if not isinstance(raw, Mapping):
            raise PolicyValidationError("Policy configuration must be a mapping")
        data = copy_tree(raw)
        try:
            return cls.model_validate(data, by_alias=True, by_name=True)
        except ValidationError as exc:
            raise PolicyValidationError(
                str(exc), errors=exc.errors(include_url=False)
            ) from exc

    def to_config(self) -> dict[str, object]:
        """Export detached ordinary containers using canonical field names."""
        from ._immutable import copy_tree

        return cast(dict[str, object], copy_tree(self, export=True))

    def with_changes(self, **changes: object) -> Self:
        """Replace top-level fields, then revalidate all domain constraints."""
        unknown = changes.keys() - type(self).model_fields.keys()
        if unknown:
            raise PolicyValidationError(f"Unknown policy fields: {sorted(unknown)}")
        data = self.to_config()
        data.update(changes)
        return type(self).from_config(data)
