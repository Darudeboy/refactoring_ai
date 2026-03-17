from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from typing import Any

from agent_automatic.domain.release.models import ReleaseProfile
from agent_automatic.domain.release.checks import distribution_from_related_issues, extract_rqg_comment_signals


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _contains_any(text: str, keywords: list[str]) -> bool:
    lowered = _norm(text)
    return any(_norm(word) in lowered for word in (keywords or []))


def _extract_issue_text(issue: dict) -> str:
    fields = issue.get("fields", {}) or {}
    summary = str(fields.get("summary", ""))
    description = str(fields.get("description", ""))
    issue_type = str(fields.get("issuetype", {}).get("name", ""))
    return " ".join([summary, description, issue_type])


def _extract_issue_status(issue: dict) -> str:
    return str((issue.get("fields", {}) or {}).get("status", {}).get("name", "Unknown"))


def _value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join(part for part in (_value_to_text(item) for item in value) if part)
    if isinstance(value, dict):
        preferred_keys = ("value", "name", "key", "url", "href", "title", "id")
        parts: list[str] = []
        for key in preferred_keys:
            if key in value:
                piece = _value_to_text(value.get(key))
                if piece:
                    parts.append(piece)
        if parts:
            return " ".join(parts)
        return str(value)
    return str(value)


def _has_meaningful_value(value: Any) -> bool:
    text = _value_to_text(value).strip().lower()
    if not text:
        return False
    return text not in {"none", "null", "n/a", "not set", "нет", "н/д", "-", "{}", "[]"}


def _status_in(status: str, allowed_statuses: list[str]) -> bool:
    status_norm = _norm(status)
    allowed = {_norm(item) for item in (allowed_statuses or [])}
    if status_norm in allowed:
        return True
    done_markers = ("done", "closed", "resolved", "выполн", "закры")
    return any(marker in status_norm for marker in done_markers)


def _status_exact_in(status: str, allowed_statuses: list[str]) -> bool:
    status_norm = _norm(status)
    allowed = {_norm(item) for item in (allowed_statuses or [])}
    return status_norm in allowed


def _get_linked_issue_keys(issue: dict) -> list[str]:
    keys: list[str] = []
    for link in (issue.get("fields", {}) or {}).get("issuelinks", []) or []:
        outward = link.get("outwardIssue")
        inward = link.get("inwardIssue")
        if outward and outward.get("key"):
            keys.append(outward["key"])
        if inward and inward.get("key"):
            keys.append(inward["key"])
    return list(set(keys))


def _find_issue_value_by_candidates(source: dict, candidates: list[str]) -> Any:
    for field_key in candidates or []:
        value = source.get(field_key)
        if _has_meaningful_value(value):
            return value
    return None


def _flatten_issue_fields(issue: dict) -> str:
    fields = issue.get("fields", {}) or {}
    rendered = issue.get("renderedFields", {}) or {}
    parts: list[str] = []
    for key, value in fields.items():
        parts.append(f"{key}:{value}")
    for key, value in rendered.items():
        parts.append(f"{key}:{value}")
    return " ".join(parts)


def _normalize_rich_text(value: Any) -> str:
    text = _value_to_text(value)
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _find_field_value_by_display_name(
    issue: dict,
    name_keywords: list[str],
    field_name_map: dict[str, str] | None = None,
) -> Any:
    fields = issue.get("fields", {}) or {}
    names = issue.get("names", {}) or {}
    if isinstance(names, dict):
        normalized_keywords = [_norm(x) for x in (name_keywords or []) if _norm(x)]
        for field_id, display_name in names.items():
            display = _norm(str(display_name))
            if display and any(keyword in display for keyword in normalized_keywords):
                value = fields.get(field_id)
                if _has_meaningful_value(value):
                    return value

    global_map = field_name_map or {}
    for field_id, value in fields.items():
        display_name = _norm(str(global_map.get(field_id, "")))
        if display_name and any(_norm(x) in display_name for x in (name_keywords or [])):
            if _has_meaningful_value(value):
                return value
    return None


def _has_distribution_link(release_issue: dict, profile: ReleaseProfile, field_name_map: dict[str, str] | None) -> bool:
    fields = release_issue.get("fields", {}) or {}
    tab = profile.distribution_tab
    value = _find_issue_value_by_candidates(fields, tab.link_fields)
    if value is None:
        value = _find_field_value_by_display_name(release_issue, tab.link_display_keywords, field_name_map=field_name_map)
    if value is None:
        blob = _flatten_issue_fields(release_issue).lower()
        ke_markers = [_norm(x) for x in tab.ke_keywords]
        has_ke = any(marker in blob for marker in ke_markers) if ke_markers else False
        if any(marker in blob for marker in ("дистриб", "distrib", "distribution", "artifact")) and (
            "http://" in blob or "https://" in blob
        ):
            return True
        if has_ke and ("http://" in blob or "https://" in blob):
            return True
        return False
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, dict):
        return bool(value)
    return True


def _is_distribution_registered(release_issue: dict, profile: ReleaseProfile, field_name_map: dict[str, str] | None) -> bool:
    fields = release_issue.get("fields", {}) or {}
    tab = profile.distribution_tab
    value = _find_issue_value_by_candidates(fields, tab.registered_fields)
    if value is None:
        # Try by display keywords; for registered flag we accept either explicit fields or textual markers.
        value = _find_field_value_by_display_name(release_issue, tab.registered_keywords, field_name_map=field_name_map)
    if value is None:
        blob = _flatten_issue_fields(release_issue).lower()
        return any(word in blob for word in ("зарегистрирован", "registered"))
    normalized = _normalize_rich_text(value)
    candidates = tab.registered_keywords or ["зарегистрирован", "registered", "yes", "true"]
    return any(_norm(k) in normalized for k in candidates)


def _is_recommendation_by_display_name(
    issue: dict,
    field_name_map: dict[str, str] | None,
    display_keywords: list[str],
    approved_keywords: list[str],
) -> bool:
    value = _find_field_value_by_display_name(issue, display_keywords, field_name_map=field_name_map)
    if value is None:
        return False
    text = _normalize_rich_text(value)
    return any(_norm(word) in text for word in (approved_keywords or []))


def _is_recommendation_in_rendered(issue: dict, display_keywords: list[str], approved_keywords: list[str]) -> bool:
    rendered = issue.get("renderedFields", {}) or {}
    html_blob = _normalize_rich_text(rendered)
    if not html_blob:
        return False
    if not any(_norm(k) in html_blob for k in (display_keywords or [])):
        return False
    return any(
        re.search(rf"\\b{re.escape(_norm(word))}\\b", html_blob, flags=re.IGNORECASE)
        for word in (approved_keywords or [])
    )


def _is_ift_recommended_from_sber_html(issue: dict) -> bool:
    html_blob = str((issue.get("fields", {}) or {}).get("customfield_sber_test_html", "") or "")
    if not html_blob:
        return False
    positive_patterns = [
        r"ift-resolution-success",
        r"rqm-conclusion-true",
        r"\\bрекомендован\\b",
        r"\\brecommended\\b",
    ]
    return any(re.search(pattern, html_blob, flags=re.IGNORECASE) for pattern in positive_patterns)


def _is_dt_recommended_from_dev_status(dev_summary: dict[str, Any] | None) -> bool:
    if not isinstance(dev_summary, dict):
        return False
    errors = dev_summary.get("errors", [])
    config_errors = dev_summary.get("configErrors", [])
    if isinstance(errors, list) and errors:
        return False
    if isinstance(config_errors, list) and config_errors:
        return False
    summary = dev_summary.get("summary", {}) if isinstance(dev_summary.get("summary", {}), dict) else {}
    repository = summary.get("repository", {}) if isinstance(summary.get("repository", {}), dict) else {}
    overall = repository.get("overall", {}) if isinstance(repository.get("overall", {}), dict) else {}
    try:
        repo_count = int(overall.get("count", 0) or 0)
    except Exception:
        repo_count = 0
    return repo_count > 0


@dataclass(frozen=True, slots=True)
class GateResult:
    id: str
    title: str
    ok: bool
    details: dict[str, Any]


def evaluate_story_quality(
    story_key: str,
    story_issue: dict,
    linked_issues: list[tuple[str, dict]],
    profile: ReleaseProfile,
) -> dict[str, Any]:
    done_statuses = profile.done_statuses
    bt_ok = False
    arch_ok = False
    bt_details = "не найдено согласованное БТ"
    arch_details = "не найдена согласованная архитектура (или не требуется)"

    for key, issue in linked_issues or []:
        text = f"{key} {_extract_issue_text(issue)}"
        status = _extract_issue_status(issue)
        if _contains_any(text, profile.story_rules.bt_keywords) and _status_in(status, done_statuses):
            bt_ok = True
            bt_details = f"{key} ({status})"
        if _contains_any(text, profile.story_rules.arch_keywords):
            if _status_in(status, done_statuses):
                arch_ok = True
                arch_details = f"{key} ({status})"
            else:
                arch_ok = False
                arch_details = f"{key} ({status})"

    if not arch_ok and "не найдена" in arch_details:
        arch_ok = True
        arch_details = "изменения архитектуры не обнаружены"

    ok = bt_ok and arch_ok
    return {
        "issue_key": story_key,
        "issue_type": "Story",
        "ok": ok,
        "details": {"bt": bt_details, "architecture": arch_details},
        "linked_keys": _get_linked_issue_keys(story_issue),
    }


def evaluate_bug_quality(bug_key: str, bug_issue: dict, profile: ReleaseProfile) -> dict[str, Any]:
    text = f"{bug_key} {_extract_issue_text(bug_issue)}"
    status = _extract_issue_status(bug_issue)
    ok = True
    reason = "ok"

    if _contains_any(text, profile.bug_rules.ct_ift_keywords):
        allowed = profile.bug_rules.ct_ift_allowed_statuses or ["Закрыт", "Закрыто", "Closed"]
        if not _status_exact_in(status, allowed):
            ok = False
            reason = f"Для CT/IFT требуется статус 'Закрыт/Closed', сейчас: {status}"

    if ok and _contains_any(text, profile.bug_rules.prom_keywords):
        if not _status_in(status, profile.bug_rules.prom_expected_statuses):
            ok = False
            reason = f"Для ПРОМ ожидается 'Подтверждение выполнения', сейчас: {status}"

    return {"issue_key": bug_key, "issue_type": "Bug", "ok": ok, "details": {"status": status, "reason": reason}}


def evaluate_release_gates_domain(
    release_issue: dict,
    related_issues: list[dict],
    profile: ReleaseProfile,
    *,
    field_name_map: dict[str, str] | None,
    dev_summary: dict[str, Any] | None,
    qgm_ok: bool,
    qgm_message: str,
    qgm_payload: dict[str, Any] | None,
    comments: list[dict] | None,
) -> dict[str, Any]:
    story_results: list[dict[str, Any]] = []
    bug_results: list[dict[str, Any]] = []

    for issue in related_issues or []:
        issue_type = str((issue.get("fields", {}) or {}).get("issuetype", {}).get("name", "")).lower()
        key = str(issue.get("key", "")).strip()
        if not key:
            continue
        if issue_type == "story":
            story_results.append(evaluate_story_quality(key, issue, [], profile))
        elif issue_type == "bug":
            bug_results.append(evaluate_bug_quality(key, issue, profile))

    stories_ok = all(item.get("ok") for item in story_results) if story_results else True
    bugs_ok = all(item.get("ok") for item in bug_results) if bug_results else True
    quality_gate = GateResult(
        id="story_bug_quality",
        title="Качество Story/Bug",
        ok=stories_ok and bugs_ok,
        details={
            "stories_total": len(story_results),
            "stories_failed": len([x for x in story_results if not x.get("ok")]),
            "bugs_total": len(bug_results),
            "bugs_failed": len([x for x in bug_results if not x.get("ok")]),
        },
    )

    dist_link_ok = _has_distribution_link(release_issue, profile, field_name_map=field_name_map)
    dist_registered_ok = _is_distribution_registered(release_issue, profile, field_name_map=field_name_map)
    dist_from_links = distribution_from_related_issues(related_issues or [])
    dist_link_ok = dist_link_ok or dist_from_links["link_present"]
    dist_registered_ok = dist_registered_ok or dist_from_links["registered"]

    distribution_gate = GateResult(
        id="distribution_tab",
        title="Вкладка Дистрибутивы",
        ok=dist_link_ok and dist_registered_ok,
        details={
            "link_present": dist_link_ok,
            "registered": dist_registered_ok,
            "linked_distribution_issue": dist_from_links,
        },
    )

    testing = profile.testing_tab
    ift_ok = _is_recommendation_by_display_name(
        release_issue,
        field_name_map=field_name_map,
        display_keywords=testing.ift_display_keywords,
        approved_keywords=testing.ift_approved_keywords,
    )
    if not ift_ok:
        ift_ok = _is_recommendation_in_rendered(release_issue, testing.ift_display_keywords, testing.ift_approved_keywords)
    if not ift_ok:
        ift_ok = _is_ift_recommended_from_sber_html(release_issue)

    ift_gate = GateResult(
        id="testing_recommendation",
        title="Результаты тестирования / рекомендация ИФТ",
        ok=ift_ok,
        details={"recommended": ift_ok},
    )

    nt_ok = _is_recommendation_by_display_name(
        release_issue,
        field_name_map=field_name_map,
        display_keywords=testing.nt_display_keywords,
        approved_keywords=testing.nt_approved_keywords,
    )
    if not nt_ok:
        nt_ok = _is_recommendation_in_rendered(release_issue, testing.nt_display_keywords, testing.nt_approved_keywords)
    nt_gate = GateResult(id="nt_recommendation", title="Рекомендация НТ", ok=nt_ok, details={"recommended": nt_ok})

    dt_ok = _is_recommendation_by_display_name(
        release_issue,
        field_name_map=field_name_map,
        display_keywords=testing.dt_display_keywords,
        approved_keywords=testing.dt_approved_keywords,
    )
    if not dt_ok:
        dt_ok = _is_recommendation_in_rendered(release_issue, testing.dt_display_keywords, testing.dt_approved_keywords)
    if not dt_ok:
        dt_ok = _is_dt_recommended_from_dev_status(dev_summary)

    dev_repo_count = (
        (((dev_summary or {}).get("summary") or {}).get("repository") or {}).get("overall") or {}
    ).get("count", 0)
    dt_gate = GateResult(
        id="dt_recommendation",
        title="Рекомендация ДТ",
        ok=dt_ok,
        details={"recommended": dt_ok, "dev_summary_repository_count": dev_repo_count},
    )

    rqg_actual_ok = False
    payload = qgm_payload or {}
    if qgm_ok and isinstance(payload, dict):
        rqg_info = payload.get("rqgInfo", {}) if isinstance(payload.get("rqgInfo", {}), dict) else {}
        has_blockers = bool(rqg_info.get("hasBlockDataRqg1") or rqg_info.get("hasBlockDataRqg2") or rqg_info.get("hasBlockDataRqg3"))
        to_comment = str(payload.get("toComment", "")).lower()
        if not has_blockers and ("успешно" in to_comment or "success" in to_comment):
            rqg_actual_ok = True
        elif not has_blockers and rqg_info:
            rqg_actual_ok = True
    if not rqg_actual_ok:
        signals = extract_rqg_comment_signals(comments or [])
        if signals.get("rqg_success"):
            rqg_actual_ok = True

    rqg_gate = GateResult(
        id="rqg_qgm",
        title="RQG (qgm endpoint)",
        ok=(rqg_actual_ok if qgm_ok else False),
        details={"ok": rqg_actual_ok, "http_ok": qgm_ok, "message": qgm_message, "payload_preview": str(payload)[:400]},
    )

    gates = [quality_gate, distribution_gate, ift_gate, nt_gate, dt_gate, rqg_gate]
    auto_passed = [g for g in gates if g.ok]
    auto_failed = [g for g in gates if not g.ok]

    return {
        "auto_passed": [g.__dict__ for g in auto_passed],
        "auto_failed": [g.__dict__ for g in auto_failed],
        "story_results": story_results,
        "bug_results": bug_results,
        "rqg_qgm": {"ok": qgm_ok, "message": qgm_message, "payload": payload},
    }

