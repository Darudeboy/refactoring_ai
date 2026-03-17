from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_automatic.domain.release.models import (
    BugRules,
    DistributionTabRules,
    ManualCheck,
    ReleaseProfile,
    StoryRules,
    TestingTabRules,
)


def _split_csv(value: str, fallback: list[str]) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return fallback
    return [part.strip() for part in raw.split(",") if part.strip()]


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Missing dependency 'pyyaml'. Install it to load YAML configs.") from e
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Profile YAML must be a mapping: {path}")
    return data


def resolve_profile_name(project_key: str, requested_profile: str, hotfix_projects: set[str]) -> str:
    requested = (requested_profile or "auto").strip().lower()
    if requested and requested != "auto":
        return requested
    if (project_key or "").strip().upper() in (hotfix_projects or set()):
        return "hotfix"
    return "default"


def load_profiles(profiles_dir: Path) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for name in ("default", "hotfix"):
        path = profiles_dir / f"{name}.yaml"
        if path.exists():
            profiles[name] = _load_yaml(path)
    return profiles


def get_release_profile(
    profiles_dir: Path,
    *,
    project_key: str,
    requested_profile: str,
    hotfix_projects: set[str],
) -> ReleaseProfile:
    profiles = load_profiles(profiles_dir)
    resolved = resolve_profile_name(project_key, requested_profile, hotfix_projects)
    raw = profiles.get(resolved) or profiles.get("default") or {}

    overrides_raw = os.getenv("RELEASE_FLOW_PROFILE_OVERRIDES", "").strip()
    if overrides_raw:
        try:
            overrides = json.loads(overrides_raw)
            if isinstance(overrides, dict) and isinstance(overrides.get(resolved), dict):
                raw = _merge_dict(raw, overrides[resolved])
        except Exception:
            pass

    name = str(raw.get("name") or resolved)
    workflow_order = list(raw.get("workflow_order") or [])
    terminal_statuses = list(raw.get("terminal_statuses") or [])
    transition_aliases = dict(raw.get("transition_aliases") or {})
    transition_ids = {str(k): str(v) for k, v in (raw.get("transition_ids") or {}).items()}

    story_raw = raw.get("story_rules") or {}
    bug_raw = raw.get("bug_rules") or {}
    testing_raw = raw.get("testing_tab") or {}
    distribution_raw = raw.get("distribution_tab") or {}

    manual_checks = []
    for item in raw.get("manual_checks") or []:
        if not isinstance(item, dict):
            continue
        manual_checks.append(
            ManualCheck(
                id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                keywords=list(item.get("keywords") or []),
                required_statuses=list(item.get("required_statuses") or []),
                required=bool(item.get("required") or False),
            )
        )

    profile = ReleaseProfile(
        name=name,
        workflow_order=workflow_order,
        terminal_statuses=terminal_statuses,
        transition_aliases={str(k): list(v or []) for k, v in transition_aliases.items()},
        transition_ids=transition_ids,
        done_statuses=list(raw.get("done_statuses") or []),
        story_rules=StoryRules(
            bt_keywords=list(story_raw.get("bt_keywords") or []),
            arch_keywords=list(story_raw.get("arch_keywords") or []),
        ),
        bug_rules=BugRules(
            ct_ift_keywords=list(bug_raw.get("ct_ift_keywords") or []),
            ct_ift_allowed_statuses=list(bug_raw.get("ct_ift_allowed_statuses") or []),
            prom_keywords=list(bug_raw.get("prom_keywords") or []),
            prom_expected_statuses=list(bug_raw.get("prom_expected_statuses") or []),
        ),
        testing_tab=TestingTabRules(
            ift_recommendation_fields=_split_csv(
                os.getenv("RELEASE_FLOW_IFT_RECOMMENDATION_FIELDS", ""),
                list(testing_raw.get("ift_recommendation_fields") or []),
            ),
            ift_display_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_IFT_DISPLAY_KEYWORDS", ""),
                list(testing_raw.get("ift_display_keywords") or []),
            ),
            ift_approved_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_IFT_APPROVED_KEYWORDS", ""),
                list(testing_raw.get("ift_approved_keywords") or []),
            ),
            nt_display_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_NT_DISPLAY_KEYWORDS", ""),
                list(testing_raw.get("nt_display_keywords") or []),
            ),
            nt_approved_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_NT_APPROVED_KEYWORDS", ""),
                list(testing_raw.get("nt_approved_keywords") or []),
            ),
            dt_display_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_DT_DISPLAY_KEYWORDS", ""),
                list(testing_raw.get("dt_display_keywords") or []),
            ),
            dt_approved_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_DT_APPROVED_KEYWORDS", ""),
                list(testing_raw.get("dt_approved_keywords") or []),
            ),
            green_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_RECOMMENDED_GREEN_KEYWORDS", ""),
                list(testing_raw.get("green_keywords") or []),
            ),
        ),
        distribution_tab=DistributionTabRules(
            link_fields=_split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_LINK_FIELDS", ""),
                list(distribution_raw.get("link_fields") or []),
            ),
            link_display_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_LINK_DISPLAY_KEYWORDS", ""),
                list(distribution_raw.get("link_display_keywords") or []),
            ),
            registered_fields=_split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_REGISTERED_FIELDS", ""),
                list(distribution_raw.get("registered_fields") or []),
            ),
            registered_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_REGISTERED_KEYWORDS", ""),
                list(distribution_raw.get("registered_keywords") or []),
            ),
            ke_keywords=_split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_KE_KEYWORDS", ""),
                list(distribution_raw.get("ke_keywords") or []),
            ),
        ),
        manual_checks=manual_checks,
        extras={k: v for k, v in raw.items() if k not in {"story_rules", "bug_rules", "testing_tab", "distribution_tab", "manual_checks"}},
    )
    # Keep ability to serialize/debug easily
    profile.extras.setdefault("_raw", raw)
    profile.extras.setdefault("_asdict", asdict(profile))
    return profile

