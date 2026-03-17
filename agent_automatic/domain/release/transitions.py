from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolvedTransition:
    id: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class ReleaseWorkflowPlan:
    release_key: str
    current_status: str
    expected_next_status: str | None
    resolved_transition: ResolvedTransition
    profile_name: str
    explain: list[str]

