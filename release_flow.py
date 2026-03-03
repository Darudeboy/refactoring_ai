from __future__ import annotations

from typing import Any, Dict, List, Optional

from release_flow_config import get_release_flow_profile


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _contains_any(text: str, keywords: List[str]) -> bool:
    lowered = _norm(text)
    return any(_norm(word) in lowered for word in (keywords or []))


def _status_in(status: str, allowed_statuses: List[str]) -> bool:
    status_norm = _norm(status)
    allowed = {_norm(item) for item in (allowed_statuses or [])}
    if status_norm in allowed:
        return True
    # Jira статусы часто отличаются формой слова: "Выполнен/Выполнено", "Закрыт/Закрыто".
    done_markers = ("done", "closed", "resolved", "выполн", "закры")
    if any(marker in status_norm for marker in done_markers):
        return True
    return False


def _extract_issue_text(issue: dict) -> str:
    fields = issue.get("fields", {}) or {}
    summary = str(fields.get("summary", ""))
    description = str(fields.get("description", ""))
    issue_type = str(fields.get("issuetype", {}).get("name", ""))
    return " ".join([summary, description, issue_type])


def _extract_issue_status(issue: dict) -> str:
    return str(issue.get("fields", {}).get("status", {}).get("name", "Unknown"))


def _extract_issue_type(issue: dict) -> str:
    return str(issue.get("fields", {}).get("issuetype", {}).get("name", ""))


def _get_linked_issue_keys(issue: dict) -> List[str]:
    keys: List[str] = []
    for link in issue.get("fields", {}).get("issuelinks", []) or []:
        outward = link.get("outwardIssue")
        inward = link.get("inwardIssue")
        if outward and outward.get("key"):
            keys.append(outward["key"])
        if inward and inward.get("key"):
            keys.append(inward["key"])
    return list(set(keys))


def _find_issue_value_by_candidates(source: dict, candidates: List[str]) -> Any:
    for field_key in candidates or []:
        value = source.get(field_key)
        if value not in (None, "", [], {}):
            return value
    return None


def _flatten_issue_fields(issue: dict) -> str:
    fields = issue.get("fields", {}) or {}
    rendered = issue.get("renderedFields", {}) or {}
    parts: List[str] = []
    for key, value in fields.items():
        parts.append(f"{key}:{value}")
    for key, value in rendered.items():
        parts.append(f"{key}:{value}")
    return " ".join(parts)


def _has_distribution_link(release_issue: dict, profile: dict) -> bool:
    fields = release_issue.get("fields", {}) or {}
    tab = profile.get("distribution_tab", {})
    candidates = tab.get("link_fields", [])
    value = _find_issue_value_by_candidates(fields, candidates)
    if value in (None, ""):
        # Fallback: в некоторых тенантах нет стабильного customfield, ищем по текстовым маркерам.
        blob = _flatten_issue_fields(release_issue).lower()
        ke_markers = [_norm(x) for x in tab.get("ke_keywords", [])]
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


def _is_distribution_registered(release_issue: dict, profile: dict) -> bool:
    fields = release_issue.get("fields", {}) or {}
    tab = profile.get("distribution_tab", {})
    value = _find_issue_value_by_candidates(fields, tab.get("registered_fields", []))
    if isinstance(value, bool):
        return value
    if value is None:
        blob = _flatten_issue_fields(release_issue).lower()
        ke_markers = [_norm(x) for x in tab.get("ke_keywords", [])]
        has_ke = any(marker in blob for marker in ke_markers) if ke_markers else False
        has_registered = any(marker in blob for marker in ("зарегистр", "registered", "регистрац"))
        return has_ke and has_registered
    return _contains_any(str(value), tab.get("registered_keywords", []))


def _is_ift_recommended(release_issue: dict, profile: dict) -> bool:
    fields = release_issue.get("fields", {}) or {}
    rendered = release_issue.get("renderedFields", {}) or {}
    tab = profile.get("testing_tab", {})
    candidates = tab.get("recommendation_fields", [])

    value = _find_issue_value_by_candidates(fields, candidates)
    if value is None:
        value = _find_issue_value_by_candidates(rendered, candidates)
    if value is None:
        blob = _flatten_issue_fields(release_issue).lower()
        has_label = "ифт" in blob or "ift" in blob
        has_recommended = "рекоменд" in blob or "recommended" in blob
        has_green = any(_norm(x) in blob for x in tab.get("green_keywords", []))
        return has_label and has_recommended and (has_green or has_recommended)
    keyword = tab.get("recommended_keyword", "рекомендован")
    value_str = str(value)
    if _contains_any(value_str, [keyword]):
        return True
    return _contains_any(value_str, ["рекоменд", "recommended"])


def _evaluate_story(jira_service, story_key: str, story_issue: dict, profile: dict) -> Dict[str, Any]:
    story_rules = profile.get("story_rules", {})
    done_statuses = profile.get("done_statuses", [])

    related_keys = _get_linked_issue_keys(story_issue)
    related_issues = []
    for key in related_keys:
        issue = jira_service.get_issue_details(key)
        if issue:
            related_issues.append((key, issue))

    bt_ok = False
    arch_ok = False
    bt_details = "не найдено согласованное БТ"
    arch_details = "не найдена согласованная архитектура (или не требуется)"

    for key, issue in related_issues:
        text = f"{key} {_extract_issue_text(issue)}"
        status = _extract_issue_status(issue)
        if _contains_any(text, story_rules.get("bt_keywords", [])) and _status_in(status, done_statuses):
            bt_ok = True
            bt_details = f"{key} ({status})"
        if _contains_any(text, story_rules.get("arch_keywords", [])):
            if _status_in(status, done_statuses):
                arch_ok = True
                arch_details = f"{key} ({status})"
            else:
                arch_ok = False
                arch_details = f"{key} ({status})"

    if not arch_ok and "не найдена" in arch_details:
        # В ряде релизов архитектурные изменения не требуются.
        arch_ok = True
        arch_details = "изменения архитектуры не обнаружены"

    ok = bt_ok and arch_ok
    return {
        "issue_key": story_key,
        "issue_type": "Story",
        "ok": ok,
        "details": {
            "bt": bt_details,
            "architecture": arch_details,
        },
    }


def _evaluate_bug(bug_key: str, bug_issue: dict, profile: dict) -> Dict[str, Any]:
    bug_rules = profile.get("bug_rules", {})
    done_statuses = profile.get("done_statuses", [])
    text = f"{bug_key} {_extract_issue_text(bug_issue)}"
    status = _extract_issue_status(bug_issue)

    ok = True
    reason = "ok"

    if _contains_any(text, bug_rules.get("ct_ift_keywords", [])):
        if not _status_in(status, done_statuses):
            ok = False
            reason = f"Для CT/IFT требуется закрытый статус, сейчас: {status}"

    if ok and _contains_any(text, bug_rules.get("prom_keywords", [])):
        prom_statuses = bug_rules.get("prom_expected_statuses", [])
        if not _status_in(status, prom_statuses):
            ok = False
            reason = f"Для ПРОМ ожидается 'Подтверждение выполнения', сейчас: {status}"

    return {
        "issue_key": bug_key,
        "issue_type": "Bug",
        "ok": ok,
        "details": {"status": status, "reason": reason},
    }


def _evaluate_manual_subtasks(release_issue: dict, related_issues: List[dict], profile: dict) -> List[Dict[str, Any]]:
    status_by_keyword: List[Dict[str, str]] = []
    all_issues = [release_issue] + related_issues
    for issue in all_issues:
        fields = issue.get("fields", {}) or {}
        for sub in fields.get("subtasks", []) or []:
            sub_summary = str(sub.get("fields", {}).get("summary", ""))
            sub_status = str(sub.get("fields", {}).get("status", {}).get("name", "Unknown"))
            status_by_keyword.append({"summary": sub_summary, "status": sub_status, "key": sub.get("key", "")})

    pending: List[Dict[str, Any]] = []
    for check in profile.get("manual_checks", []):
        keywords = check.get("keywords", [])
        required_statuses = check.get("required_statuses", [])
        if not keywords:
            pending.append(
                {
                    "id": check.get("id"),
                    "title": check.get("title"),
                    "status": "manual",
                    "message": "Требуется ручное подтверждение.",
                }
            )
            continue

        matched = [item for item in status_by_keyword if _contains_any(item.get("summary", ""), keywords)]
        if not matched:
            pending.append(
                {
                    "id": check.get("id"),
                    "title": check.get("title"),
                    "status": "optional_missing",
                    "message": "Подзадача не найдена (проверь, требуется ли для проекта).",
                }
            )
            continue

        bad = [item for item in matched if not _status_in(item.get("status", ""), required_statuses)]
        if bad:
            pending.append(
                {
                    "id": check.get("id"),
                    "title": check.get("title"),
                    "status": "manual",
                    "message": f"Есть незакрытые подзадачи: {', '.join(x.get('key') or x.get('summary', '') for x in bad)}",
                }
            )

    return pending


def _derive_business_project(release_issue: dict, related_issues: List[dict]) -> str:
    # Для отчета берем проект по первой Story/Bug в составе релиза, а не проект релизной задачи.
    for issue in related_issues:
        issue_type = _extract_issue_type(issue).lower()
        if issue_type in ("story", "bug", "история", "дефект"):
            project_key = str(issue.get("fields", {}).get("project", {}).get("key", "")).strip().upper()
            if project_key:
                return project_key
    return str(release_issue.get("fields", {}).get("project", {}).get("key", "")).strip().upper()


def _next_transition(current_status: str, workflow_order: List[str]) -> Optional[str]:
    normalized = [_norm(x) for x in workflow_order]
    current = _norm(current_status)
    if current not in normalized:
        return None
    idx = normalized.index(current)
    if idx >= len(workflow_order) - 1:
        return None
    return workflow_order[idx + 1]


def evaluate_release_gates(
    jira_service,
    release_key: str,
    profile_name: str = "auto",
    manual_confirmations: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    safe_release = (release_key or "").strip().upper()
    if not safe_release:
        return {"success": False, "message": "Не указан release_key."}

    release = jira_service.get_issue_details(safe_release)
    if not release:
        return {"success": False, "message": f"Релиз {safe_release} не найден."}

    release_project_key = str(release.get("fields", {}).get("project", {}).get("key", ""))
    profile = get_release_flow_profile(project_key=release_project_key, requested_profile=profile_name)

    linked_keys = jira_service.get_linked_issues(safe_release)
    related_issues: List[dict] = []
    story_results: List[Dict[str, Any]] = []
    bug_results: List[Dict[str, Any]] = []

    for key in linked_keys:
        issue = jira_service.get_issue_details(key)
        if not issue:
            continue
        related_issues.append(issue)
        issue_type = _extract_issue_type(issue).lower()
        if issue_type == "story":
            story_results.append(_evaluate_story(jira_service, key, issue, profile))
        elif issue_type == "bug":
            bug_results.append(_evaluate_bug(key, issue, profile))

    project_key = _derive_business_project(release, related_issues)

    auto_passed: List[Dict[str, Any]] = []
    auto_failed: List[Dict[str, Any]] = []

    stories_ok = all(item.get("ok") for item in story_results) if story_results else True
    bugs_ok = all(item.get("ok") for item in bug_results) if bug_results else True
    quality_gate = {
        "id": "story_bug_quality",
        "title": "Качество Story/Bug",
        "ok": stories_ok and bugs_ok,
        "details": {
            "stories_total": len(story_results),
            "stories_failed": len([x for x in story_results if not x.get("ok")]),
            "bugs_total": len(bug_results),
            "bugs_failed": len([x for x in bug_results if not x.get("ok")]),
        },
    }
    (auto_passed if quality_gate["ok"] else auto_failed).append(quality_gate)

    dist_link_ok = _has_distribution_link(release, profile)
    dist_registered_ok = _is_distribution_registered(release, profile)
    recommendation_ok = _is_ift_recommended(release, profile)

    dist_gate = {
        "id": "distribution_tab",
        "title": "Вкладка Дистрибутивы",
        "ok": dist_link_ok and dist_registered_ok,
        "details": {
            "link_present": dist_link_ok,
            "registered": dist_registered_ok,
        },
    }
    (auto_passed if dist_gate["ok"] else auto_failed).append(dist_gate)

    recommendation_gate = {
        "id": "testing_recommendation",
        "title": "Результаты тестирования / рекомендация ИФТ",
        "ok": recommendation_ok,
        "details": {"recommended": recommendation_ok},
    }
    (auto_passed if recommendation_gate["ok"] else auto_failed).append(recommendation_gate)

    manual_raw = _evaluate_manual_subtasks(release, related_issues, profile)
    manual_pending = [item for item in manual_raw if item.get("status") != "optional_missing"]
    manual_optional = [item for item in manual_raw if item.get("status") == "optional_missing"]

    confirmations = manual_confirmations or {}
    manual_done = []
    still_pending = []
    for item in manual_pending:
        check_id = item.get("id")
        if confirmations.get(check_id) is True:
            manual_done.append(item)
        else:
            still_pending.append(item)
    manual_pending = still_pending

    current_status = _extract_issue_status(release)
    next_status = _next_transition(current_status, profile.get("workflow_order", []))

    ready_for_transition = len(auto_failed) == 0 and len(manual_pending) == 0 and bool(next_status)

    return {
        "success": True,
        "release_key": safe_release,
        "project_key": project_key,
        "profile_name": profile.get("name", "default"),
        "current_stage": current_status,
        "next_allowed_transition": next_status,
        "ready_for_transition": ready_for_transition,
        "auto_passed": auto_passed,
        "auto_failed": auto_failed,
        "manual_pending": manual_pending,
        "manual_optional": manual_optional,
        "manual_done": manual_done,
        "story_results": story_results,
        "bug_results": bug_results,
    }


def format_release_gate_report(result: Dict[str, Any]) -> str:
    if not result.get("success"):
        return f"❌ {result.get('message', 'Ошибка оценки гейтов')}"

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append(f"🧭 GUIDED CYCLE: {result.get('release_key')}")
    lines.append("=" * 80)
    lines.append(f"Профиль: {result.get('profile_name')} | Проект: {result.get('project_key')}")
    lines.append(f"Текущий этап: {result.get('current_stage')}")
    lines.append(f"Следующий этап: {result.get('next_allowed_transition') or 'нет'}")
    lines.append("")

    lines.append(f"✅ Авто-гейты пройдены: {len(result.get('auto_passed', []))}")
    for gate in result.get("auto_passed", []):
        lines.append(f"  - {gate.get('title')}")
    lines.append(f"❌ Авто-гейты провалены: {len(result.get('auto_failed', []))}")
    for gate in result.get("auto_failed", []):
        lines.append(f"  - {gate.get('title')}: {gate.get('details')}")
    lines.append("")

    lines.append(f"📝 Ручные проверки pending: {len(result.get('manual_pending', []))}")
    for check in result.get("manual_pending", []):
        lines.append(f"  - {check.get('id')}: {check.get('message')}")
    if result.get("manual_done"):
        lines.append(f"✅ Подтверждено вручную: {len(result.get('manual_done', []))}")
        for check in result.get("manual_done", []):
            lines.append(f"  - {check.get('id')}: подтверждено")
    if result.get("manual_optional"):
        lines.append(f"ℹ️ Опциональные проверки: {len(result.get('manual_optional', []))}")
        for check in result.get("manual_optional", []):
            lines.append(f"  - {check.get('id')}: {check.get('message')}")
    lines.append("")

    if result.get("ready_for_transition"):
        lines.append("🚀 Готов к переходу по workflow.")
    else:
        lines.append("⛔ Переход пока заблокирован (есть непройденные гейты или ручные проверки).")

    lines.append("=" * 80)
    return "\n".join(lines)
