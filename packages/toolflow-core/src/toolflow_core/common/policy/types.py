"""Published policy snapshots."""

from dataclasses import dataclass

from .base import BasePolicy


@dataclass(frozen=True, slots=True)
class StoredPolicy[TPolicy: BasePolicy]:
    """A complete policy paired with its authoritative persistence version."""

    policy: TPolicy
    source_version: str


@dataclass(frozen=True, slots=True, eq=False)
class PolicySnapshot[TPolicy: BasePolicy]:
    """A stable policy value with separate local and persistence versions.

    Keep the actual snapshot object as the condition for a subsequent update.
    Revisions do not establish ordering across managers or processes.
    """

    revision: int
    policy: TPolicy
    source_version: str
