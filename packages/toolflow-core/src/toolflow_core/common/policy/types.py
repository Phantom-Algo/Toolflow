"""Published policy snapshots."""

from dataclasses import dataclass

from .base import BasePolicy


@dataclass(frozen=True, slots=True, eq=False)
class PolicySnapshot[TPolicy: BasePolicy]:
    """A stable policy value and its manager-local publication revision.

    Keep the actual snapshot object as the condition for a subsequent update.
    Revisions do not establish ordering across managers or processes.
    """

    revision: int
    policy: TPolicy
