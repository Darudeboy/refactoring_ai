from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GuidedCycleReport:
    release_key: str
    project_key: str
    profile_name: str
    current_stage: str
    next_allowed_transition: str | None
    next_allowed_transition_id: str | None
    is_terminal_status: bool
    ready_for_transition: bool
    cycle_completed: bool
    auto_passed: list[dict[str, Any]] = field(default_factory=list)
    auto_failed: list[dict[str, Any]] = field(default_factory=list)
    manual_pending: list[dict[str, Any]] = field(default_factory=list)
    manual_optional: list[dict[str, Any]] = field(default_factory=list)
    manual_done: list[dict[str, Any]] = field(default_factory=list)
    story_results: list[dict[str, Any]] = field(default_factory=list)
    bug_results: list[dict[str, Any]] = field(default_factory=list)
    rqg_qgm: dict[str, Any] = field(default_factory=dict)

