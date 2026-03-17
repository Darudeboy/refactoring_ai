from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ManualCheck:
    id: str
    title: str
    keywords: list[str] = field(default_factory=list)
    required_statuses: list[str] = field(default_factory=list)
    required: bool = False


@dataclass(frozen=True, slots=True)
class StoryRules:
    bt_keywords: list[str] = field(default_factory=list)
    arch_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BugRules:
    ct_ift_keywords: list[str] = field(default_factory=list)
    ct_ift_allowed_statuses: list[str] = field(default_factory=list)
    prom_keywords: list[str] = field(default_factory=list)
    prom_expected_statuses: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TestingTabRules:
    ift_recommendation_fields: list[str] = field(default_factory=list)
    ift_display_keywords: list[str] = field(default_factory=list)
    ift_approved_keywords: list[str] = field(default_factory=list)
    nt_display_keywords: list[str] = field(default_factory=list)
    nt_approved_keywords: list[str] = field(default_factory=list)
    dt_display_keywords: list[str] = field(default_factory=list)
    dt_approved_keywords: list[str] = field(default_factory=list)
    green_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DistributionTabRules:
    link_fields: list[str] = field(default_factory=list)
    link_display_keywords: list[str] = field(default_factory=list)
    registered_fields: list[str] = field(default_factory=list)
    registered_keywords: list[str] = field(default_factory=list)
    ke_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReleaseProfile:
    name: str
    workflow_order: list[str]
    terminal_statuses: list[str] = field(default_factory=list)
    transition_aliases: dict[str, list[str]] = field(default_factory=dict)
    transition_ids: dict[str, str] = field(default_factory=dict)

    done_statuses: list[str] = field(default_factory=list)
    story_rules: StoryRules = field(default_factory=StoryRules)
    bug_rules: BugRules = field(default_factory=BugRules)
    testing_tab: TestingTabRules = field(default_factory=TestingTabRules)
    distribution_tab: DistributionTabRules = field(default_factory=DistributionTabRules)
    manual_checks: list[ManualCheck] = field(default_factory=list)

    extras: dict[str, Any] = field(default_factory=dict)

