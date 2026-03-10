from __future__ import annotations

import re
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


def _status_exact_in(status: str, allowed_statuses: List[str]) -> bool:
    status_norm = _norm(status)
    allowed = {_norm(item) for item in (allowed_statuses or [])}
    return status_norm in allowed


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
        parts: List[str] = []
        for key in preferred_keys:
            if key in value:
                piece = _value_to_text(value.get(key))
                if piece:
                    parts.append(piece)
        if parts:
            return " ".join(parts)
        # Fallback для ADF/нестандартных структур.
        return str(value)
    return str(value)


def _has_meaningful_value(value: Any) -> bool:
    text = _value_to_text(value).strip().lower()
    if not text:
        return False
    return text not in {"none", "null", "n/a", "not set", "нет", "н/д", "-", "{}", "[]"}


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
        if _has_meaningful_value(value):
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


def _find_field_value_by_display_name(
    issue: dict,
    name_keywords: List[str],
    field_name_map: Optional[Dict[str, str]] = None,
) -> Any:
    """Ищет значение customfield по человекочитаемому имени поля (expand=names)."""
    fields = issue.get("fields", {}) or {}
    names = issue.get("names", {}) or {}
    if not isinstance(names, dict):
        return None

    normalized_keywords = [_norm(x) for x in (name_keywords or []) if _norm(x)]
    for field_id, display_name in names.items():
        display = _norm(str(display_name))
        if not display:
            continue
        if any(keyword in display for keyword in normalized_keywords):
            value = fields.get(field_id)
            if _has_meaningful_value(value):
                return value

    # Fallback: если expand=names не вернулось, используем глобальную карту полей Jira.
    global_map = field_name_map or {}
    for field_id, value in fields.items():
        display_name = _norm(str(global_map.get(field_id, "")))
        if not display_name:
            continue
        if any(keyword in display_name for keyword in normalized_keywords):
            if _has_meaningful_value(value):
                return value
    return None


def _has_distribution_link(release_issue: dict, profile: dict) -> bool:
    fields = release_issue.get("fields", {}) or {}
    tab = profile.get("distribution_tab", {})
    candidates = tab.get("link_fields", [])
    value = _find_issue_value_by_candidates(fields, candidates)
    if value is None:
        # Приоритетно читаем поле по display-name: "Ссылка на дистрибутив".
        value = _find_field_value_by_display_name(
            release_issue,
            tab.get("link_display_keywords", []),
        )
    if value is None:
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


def _is_distribution_registered(
    release_issue: dict,
    profile: dict,
    field_name_map: Optional[Dict[str, str]] = None,
) -> bool:
    fields = release_issue.get("fields", {}) or {}
    tab = profile.get("distribution_tab", {})
    value = _find_issue_value_by_candidates(fields, tab.get("registered_fields", []))
    if value is None:
        # Приоритетно: строка "КЭ дистрибутива" (display-name поля из Jira).
        value = _find_field_value_by_display_name(
            release_issue,
            tab.get("ke_keywords", []),
            field_name_map=field_name_map,
        )
    if isinstance(value, bool):
        return value
    if value is None:
        blob = _flatten_issue_fields(release_issue).lower()
        ke_markers = [_norm(x) for x in tab.get("ke_keywords", [])]
        has_ke = any(marker in blob for marker in ke_markers) if ke_markers else False
        has_registered = any(marker in blob for marker in ("зарегистр", "registered", "регистрац"))
        if has_ke and has_registered:
            return True

        # Jira UI-кейс: "КЭ дистрибутива" заполняется только после регистрации.
        # Если поле КЭ найдено и рядом нет явного "нет/н/д", считаем зарегистрированным.
        if has_ke:
            negative_patterns = (
                r"кэ дистрибутива[^a-zа-я0-9]{0,20}(нет|н/д|n/a|none)",
                r"ke distribution[^a-z0-9]{0,20}(no|n/a|none|not set)",
            )
            if not any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in negative_patterns):
                return True
        return False
    value_text = _value_to_text(value)
    if _contains_any(value_text, tab.get("registered_keywords", [])):
        return True
    # Если в КЭ стоит конкретное значение версии/объекта (не "Н/Д"), считаем зарегистрированным.
    if not re.search(r"\b(н/д|нет|none|n/a|not set)\b", value_text, flags=re.IGNORECASE):
        return True
    return False


def _is_ift_recommended(release_issue: dict, profile: dict) -> bool:
    fields = release_issue.get("fields", {}) or {}
    rendered = release_issue.get("renderedFields", {}) or {}
    tab = profile.get("testing_tab", {})
    candidates = tab.get("ift_recommendation_fields", [])

    value = _find_issue_value_by_candidates(fields, candidates)
    if value is None:
        value = _find_issue_value_by_candidates(rendered, candidates)
    if value is None:
        value = _find_field_value_by_display_name(
            release_issue,
            tab.get("ift_display_keywords", []),
        )
    if value is None:
        blob = _flatten_issue_fields(release_issue).lower()
        has_label = "ифт" in blob or "ift" in blob
        has_recommended = "рекоменд" in blob or "recommended" in blob
        has_green = any(_norm(x) in blob for x in tab.get("green_keywords", []))
        if has_label and has_recommended and (has_green or has_recommended):
            return True

        # Дополнительный парсинг для rendered HTML:
        # "Рекомендация по отчету ИФТ ... Рекомендован".
        html_blob = str(release_issue.get("renderedFields", {})).lower()
        if re.search(
            r"рекомендац[а-я\s]*по\s*отчет[а-я\s]*ифт.{0,400}рекомендован",
            html_blob,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            return True
        if re.search(
            r"ift.{0,200}recommend.{0,200}recommend",
            html_blob,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            return True
        return False
    keyword = (tab.get("ift_approved_keywords") or ["рекомендован"])[0]
    value_str = str(value)
    if _contains_any(value_str, [keyword]):
        return True
    return _contains_any(value_str, tab.get("ift_approved_keywords", ["рекоменд", "recommended"]))


def _is_recommendation_by_display_name(
    release_issue: dict,
    field_name_map: Dict[str, str],
    display_keywords: List[str],
    approved_keywords: List[str],
) -> bool:
    value = _find_field_value_by_display_name(
        release_issue,
        display_keywords,
        field_name_map=field_name_map,
    )
    if value is None:
        blob = _flatten_issue_fields(release_issue).lower()
        has_marker = any(_norm(k) in blob for k in (display_keywords or []))
        if has_marker:
            return _contains_any(blob, approved_keywords)
        return False
    return _contains_any(_value_to_text(value), approved_keywords)


def _is_recommendation_in_rendered(
    release_issue: dict,
    label_patterns: List[str],
    approved_keywords: List[str],
) -> bool:
    rendered = release_issue.get("renderedFields", {}) or {}
    html_blob = str(rendered).lower()
    for label in label_patterns or []:
        label_norm = _norm(label)
        if not label_norm:
            continue
        # Допускаем "лейбл ... значение" в пределах одного визуального блока.
        pattern = rf"{re.escape(label_norm)}.{0,250}({'|'.join(re.escape(_norm(k)) for k in approved_keywords if _norm(k))})"
        if re.search(pattern, html_blob, flags=re.IGNORECASE | re.DOTALL):
            return True
    return False


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
    text = f"{bug_key} {_extract_issue_text(bug_issue)}"
    status = _extract_issue_status(bug_issue)

    ok = True
    reason = "ok"

    if _contains_any(text, bug_rules.get("ct_ift_keywords", [])):
        # По ТЗ bug CT/IFT должен быть именно "Закрыт/Closed".
        ct_ift_allowed = bug_rules.get("ct_ift_allowed_statuses", ["Закрыт", "Закрыто", "Closed"])
        if not _status_exact_in(status, ct_ift_allowed):
            ok = False
            reason = f"Для CT/IFT требуется статус 'Закрыт/Closed', сейчас: {status}"

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
        required = bool(check.get("required", False))
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
                    "status": "manual" if required else "optional_missing",
                    "message": (
                        "Подзадача не найдена, проверка блокирует переход."
                        if required
                        else "Подзадача не найдена (проверь, требуется ли для проекта)."
                    ),
                }
            )
            continue

        bad = [item for item in matched if not _status_exact_in(item.get("status", ""), required_statuses)]
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


def _distribution_from_related_issues(related_issues: List[dict]) -> Dict[str, bool]:
    link_present = False
    registered = False
    dist_markers = ("дистриб", "distribution", "distrib", "release-notes", "install")
    approved_markers = ("утвержден", "approved", "согласован", "выполн", "закры")

    for issue in related_issues:
        fields = issue.get("fields", {}) or {}
        issue_type = str(fields.get("issuetype", {}).get("name", ""))
        summary = str(fields.get("summary", ""))
        status = str(fields.get("status", {}).get("name", ""))
        text = f"{issue_type} {summary}".lower()
        if any(marker in text for marker in dist_markers):
            link_present = True
            if any(marker in status.lower() for marker in approved_markers):
                registered = True
    return {"link_present": link_present, "registered": registered}


def _comment_text(comment: dict) -> str:
    body = comment.get("body", "")
    if isinstance(body, str):
        return body
    # Jira иногда возвращает body как ADF-структуру.
    return str(body)


def _extract_rqg_comment_signals(comments: List[dict]) -> Dict[str, bool]:
    text_blob = "\n".join(_comment_text(c) for c in comments).lower()
    return {
        "rqg_success": "проверки rqg успешно выполнены" in text_blob or "rqg" in text_blob and "успеш" in text_blob,
        "testing_completed": "запланированный объём тестирования: выполнен" in text_blob
        or "запланированный объем тестирования: выполнен" in text_blob,
        "no_critical_bugs": "открытые блокирующие и критичные дефекты: нет" in text_blob
        or "критичные дефекты: нет" in text_blob,
        "recommended_to_psi": "рекомендации по переводу на пси: рекомендован" in text_blob
        or "рекомендован" in text_blob and "пси" in text_blob,
    }


def _next_transition(current_status: str, workflow_order: List[str]) -> Optional[str]:
    normalized = [_norm(x) for x in workflow_order]
    current = _norm(current_status)
    if current not in normalized:
        return None
    idx = normalized.index(current)
    if idx >= len(workflow_order) - 1:
        return None
    return workflow_order[idx + 1]


def _resolve_transition_id(profile: dict, next_status: Optional[str]) -> Optional[str]:
    if not next_status:
        return None
    transition_ids = profile.get("transition_ids", {}) or {}
    transition_id = transition_ids.get(next_status)
    if transition_id is None:
        return None
    return str(transition_id)


def evaluate_release_gates(
    jira_service,
    release_key: str,
    profile_name: str = "auto",
    manual_confirmations: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    safe_release = (release_key or "").strip().upper()
    if not safe_release:
        return {"success": False, "message": "Не указан release_key."}

    release = jira_service.get_issue_details(
        safe_release,
        expand="issuelinks,renderedFields,names",
    )
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
    auto_warnings: List[Dict[str, Any]] = []

    stories_ok = all(item.get("ok") for item in story_results) if story_results else True
    story_gate = {
        "id": "story_quality",
        "title": "Качество Story (наличие БТ и Архитектуры)",
        "ok": stories_ok,
        "details": {
            "stories_total": len(story_results),
            "stories_failed": len([x for x in story_results if not x.get("ok")]),
        },
    }
    (auto_passed if story_gate["ok"] else auto_failed).append(story_gate)

    bugs_ok = all(item.get("ok") for item in bug_results) if bug_results else True
    if not bugs_ok:
        bad_bugs = [x for x in bug_results if not x.get("ok")]
        bug_warning = {
            "id": "bug_quality",
            "title": "Баг в некорректном статусе - внимание",
            "ok": False,
            "details": {
                "bugs_total": len(bug_results),
                "bugs_failed": len(bad_bugs),
                "reasons": [f"{b.get('issue_key')}: {b.get('details', {}).get('reason')}" for b in bad_bugs]
            }
        }
        auto_warnings.append(bug_warning)
    elif bug_results:
        auto_passed.append({
            "id": "bug_quality",
            "title": "Статусы багов (CT/IFT/PROM)",
            "ok": True,
            "details": {"message": "Все баги в корректных статусах"}
        })

    dist_link_ok = _has_distribution_link(release, profile)
    field_name_map = jira_service.get_field_name_map()
    dist_registered_ok = _is_distribution_registered(
        release,
        profile,
        field_name_map=field_name_map,
    )
    distribution_tab = profile.get("distribution_tab", {})
    dist_link_value = _find_field_value_by_display_name(
        release,
        distribution_tab.get("link_display_keywords", []),
        field_name_map=field_name_map,
    )
    dist_ke_value = _find_field_value_by_display_name(
        release,
        distribution_tab.get("ke_keywords", []),
        field_name_map=field_name_map,
    )
    recommendation_ok = _is_ift_recommended(release, profile)
    testing_tab = profile.get("testing_tab", {})
    nt_recommendation_ok = _is_recommendation_by_display_name(
        release,
        field_name_map=field_name_map,
        display_keywords=testing_tab.get("nt_display_keywords", []),
        approved_keywords=testing_tab.get("nt_approved_keywords", []),
    )
    if not nt_recommendation_ok:
        nt_recommendation_ok = _is_recommendation_in_rendered(
            release,
            testing_tab.get("nt_display_keywords", []),
            testing_tab.get("nt_approved_keywords", []),
        )
    dt_recommendation_ok = _is_recommendation_by_display_name(
        release,
        field_name_map=field_name_map,
        display_keywords=testing_tab.get("dt_display_keywords", []),
        approved_keywords=testing_tab.get("dt_approved_keywords", []),
    )
    if not dt_recommendation_ok:
        dt_recommendation_ok = _is_recommendation_in_rendered(
            release,
            testing_tab.get("dt_display_keywords", []),
            testing_tab.get("dt_approved_keywords", []),
        )
    if not recommendation_ok:
        recommendation_ok = _is_recommendation_in_rendered(
            release,
            testing_tab.get("ift_display_keywords", []),
            testing_tab.get("ift_approved_keywords", []),
        )

    qgm_ok, qgm_message, qgm_payload = jira_service.get_qgm_status(safe_release)

    # Дополнительный fallback:
    # если дистрибутив оформлен как отдельная связанная задача со статусом "Утвержден",
    # учитываем это как валидный признак "прилинкован + зарегистрирован".
    dist_from_links = _distribution_from_related_issues(related_issues)
    dist_link_ok = dist_link_ok or dist_from_links["link_present"]
    dist_registered_ok = dist_registered_ok or dist_from_links["registered"]

    dist_gate = {
        "id": "distribution_tab",
        "title": "Вкладка Дистрибутивы",
        "ok": dist_link_ok and dist_registered_ok,
        "details": {
            "link_present": dist_link_ok,
            "registered": dist_registered_ok,
            "distribution_link_value": _value_to_text(dist_link_value)[:300],
            "distribution_ke_value": _value_to_text(dist_ke_value)[:300],
            "linked_distribution_issue": dist_from_links,
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

    nt_gate = {
        "id": "nt_recommendation",
        "title": "Рекомендация НТ",
        "ok": nt_recommendation_ok,
        "details": {"recommended": nt_recommendation_ok},
    }
    (auto_passed if nt_gate["ok"] else auto_failed).append(nt_gate)

    dt_gate = {
        "id": "dt_recommendation",
        "title": "Рекомендация ДТ",
        "ok": dt_recommendation_ok,
        "details": {"recommended": dt_recommendation_ok},
    }
    (auto_passed if dt_gate["ok"] else auto_failed).append(dt_gate)

    rqg_gate = {
        "id": "rqg_qgm",
        "title": "RQG (qgm endpoint)",
        "ok": qgm_ok,
        "details": {
            "ok": qgm_ok,
            "message": qgm_message,
            "payload_preview": str(qgm_payload or {})[:400],
        },
    }
    (auto_passed if rqg_gate["ok"] else auto_failed).append(rqg_gate)

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
    next_transition_id = _resolve_transition_id(profile, next_status)

    ready_for_transition = len(auto_failed) == 0 and len(manual_pending) == 0 and bool(next_status)

    return {
        "success": True,
        "release_key": safe_release,
        "project_key": project_key,
        "profile_name": profile.get("name", "default"),
        "current_stage": current_status,
        "next_allowed_transition": next_status,
        "next_allowed_transition_id": next_transition_id,
        "ready_for_transition": ready_for_transition,
        "auto_passed": auto_passed,
        "auto_failed": auto_failed,
        "auto_warnings": auto_warnings,
        "manual_pending": manual_pending,
        "manual_optional": manual_optional,
        "manual_done": manual_done,
        "story_results": story_results,
        "bug_results": bug_results,
        "rqg_qgm": {"ok": qgm_ok, "message": qgm_message, "payload": qgm_payload or {}},
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
    if result.get("next_allowed_transition_id"):
        lines.append(f"Transition ID: {result.get('next_allowed_transition_id')}")
    rqg_qgm = result.get("rqg_qgm", {}) or {}
    if rqg_qgm.get("ok"):
        lines.append("RQG qgm: успешно")
    lines.append("")

    lines.append(f"✅ Авто-гейты пройдены: {len(result.get('auto_passed', []))}")
    for gate in result.get("auto_passed", []):
        lines.append(f"  - {gate.get('title')}")
    lines.append(f"❌ Авто-гейты провалены: {len(result.get('auto_failed', []))}")
    for gate in result.get("auto_failed", []):
        lines.append(f"  - {gate.get('title')}: {gate.get('details')}")
        gate_id = gate.get("id")
        if gate_id == "distribution_tab":
            lines.append("    Что сделать: проверь поля 'Ссылка на дистрибутив' и 'КЭ дистрибутива'.")
        elif gate_id == "testing_recommendation":
            lines.append("    Что сделать: в релизе должна быть рекомендация ИФТ = 'Рекомендован'.")
        elif gate_id == "nt_recommendation":
            lines.append("    Что сделать: НТ должна быть 'Не требуется' или 'Версия 2 РЕКОМЕНДОВАН'.")
        elif gate_id == "dt_recommendation":
            lines.append("    Что сделать: ДТ должна быть 'РЕКОМЕНДОВАН'.")
        elif gate_id == "rqg_qgm":
            lines.append("    Что сделать: проверь ответ /rest/release/1/qgm по issueId релиза.")
        elif gate_id == "story_bug_quality":
            lines.append("    Что сделать: закрой bug CT/IFT (только статус 'Закрыт/Closed').")
    lines.append("")

    lines.append(f"📝 Ручные проверки pending: {len(result.get('manual_pending', []))}")
    for check in result.get("manual_pending", []):
        lines.append(f"  - {check.get('id')}: {check.get('message')}")
    if result.get("manual_pending"):
        lines.append("  Подтвердить вручную можно командой:")
        lines.append(
            f"  confirm_manual_check({result.get('release_key')}, <check_id>, ok)"
        )
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
