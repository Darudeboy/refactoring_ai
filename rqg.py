import os
from typing import Dict, List, Set, Optional


def _split_csv(value: str, fallback: List[str]) -> List[str]:
    raw = (value or "").strip()
    if not raw:
        return fallback
    return [part.strip() for part in raw.split(",") if part.strip()]


def _build_rqg_settings() -> Dict[str, List[str]]:
    return {
        "co_keywords": [s.lower() for s in _split_csv(os.getenv("RQG_CO_KEYWORDS", ""), ["цо", "co"])],
        "ift_keywords": [s.lower() for s in _split_csv(os.getenv("RQG_IFT_KEYWORDS", ""), ["ифт", "ift"])],
        "distribution_keywords": [
            s.lower() for s in _split_csv(os.getenv("RQG_DISTRIBUTION_KEYWORDS", ""), ["дистриб", "distrib", "release-notes", "install"])
        ],
        "co_statuses": [s.lower() for s in _split_csv(os.getenv("RQG_CO_ALLOWED_STATUSES", ""), ["done", "closed", "resolved", "выполнено", "закрыто"])],
        "ift_statuses": [s.lower() for s in _split_csv(os.getenv("RQG_IFT_ALLOWED_STATUSES", ""), ["done", "closed", "resolved", "выполнено", "закрыто"])],
        "distribution_statuses": [
            s.lower() for s in _split_csv(os.getenv("RQG_DISTRIBUTION_ALLOWED_STATUSES", ""), ["done", "closed", "resolved", "выполнено", "закрыто"])
        ],
    }


def _issue_summary(issue: dict) -> str:
    return (issue.get("fields", {}).get("summary") or "").strip()


def _issue_status(issue: dict) -> str:
    return (issue.get("fields", {}).get("status", {}).get("name") or "").strip()


def _issue_type(issue: dict) -> str:
    return (issue.get("fields", {}).get("issuetype", {}).get("name") or "").strip()


def _linked_issue_keys(issue: dict) -> List[str]:
    keys: List[str] = []
    for link in issue.get("fields", {}).get("issuelinks", []) or []:
        outward = link.get("outwardIssue")
        inward = link.get("inwardIssue")
        if outward and outward.get("key"):
            keys.append(outward["key"])
        if inward and inward.get("key"):
            keys.append(inward["key"])
    return list(set(keys))


def _contains_any(text: str, needles: List[str]) -> bool:
    lowered = (text or "").lower()
    return any(needle in lowered for needle in needles)


def _classify_related_issue(issue_key: str, issue: dict, settings: Dict[str, List[str]]) -> str:
    summary = _issue_summary(issue)
    combined = f"{issue_key} {summary}".lower()
    issue_type = _issue_type(issue).lower()

    if _contains_any(combined, settings["co_keywords"]):
        return "co"
    if _contains_any(combined, settings["ift_keywords"]):
        return "ift"
    if _contains_any(combined, settings["distribution_keywords"]) or "дистриб" in issue_type:
        return "distribution"
    return ""


def _has_distribution_attachment(story_issue: dict, distribution_keywords: List[str]) -> bool:
    attachments = story_issue.get("fields", {}).get("attachment", []) or []
    for attachment in attachments:
        filename = (attachment.get("filename") or "").lower()
        if _contains_any(filename, distribution_keywords):
            return True
    return False


def analyze_rqg_for_release(jira_service, release_key: str, max_depth: int = 2) -> Dict:
    settings = _build_rqg_settings()
    release = jira_service.get_issue_details(release_key)
    if not release:
        return {"success": False, "message": f"Релиз {release_key} не найден"}

    # Собираем вложенные задачи релиза (BFS по связям и сабтаскам).
    discovered: Set[str] = set()
    queue: List[tuple[str, int]] = [(release_key, 0)]

    while queue:
        issue_key, depth = queue.pop(0)
        if issue_key in discovered or depth > max_depth:
            continue
        discovered.add(issue_key)

        issue = jira_service.get_issue_details(issue_key)
        if not issue:
            continue

        for subtask in issue.get("fields", {}).get("subtasks", []) or []:
            sub_key = subtask.get("key")
            if sub_key and sub_key not in discovered:
                queue.append((sub_key, depth + 1))

        for linked_key in _linked_issue_keys(issue):
            if linked_key not in discovered:
                queue.append((linked_key, depth + 1))

    discovered.discard(release_key)

    stories: List[str] = []
    for issue_key in sorted(discovered):
        issue = jira_service.get_issue_details(issue_key)
        if not issue:
            continue
        if _issue_type(issue).lower() == "story":
            stories.append(issue_key)

    story_results: List[Dict] = []
    failed = 0

    for story_key in stories:
        story_issue = jira_service.get_issue_details(story_key)
        if not story_issue:
            continue

        related_items = []
        for related_key in _linked_issue_keys(story_issue):
            related_issue = jira_service.get_issue_details(related_key)
            if not related_issue:
                continue
            related_type = _classify_related_issue(related_key, related_issue, settings)
            if related_type:
                related_items.append({
                    "key": related_key,
                    "type": related_type,
                    "status": _issue_status(related_issue),
                    "summary": _issue_summary(related_issue),
                })

        co_items = [x for x in related_items if x["type"] == "co"]
        ift_items = [x for x in related_items if x["type"] == "ift"]
        dist_items = [x for x in related_items if x["type"] == "distribution"]

        co_ok = bool(co_items) and all((x["status"] or "").lower() in settings["co_statuses"] for x in co_items)
        ift_ok = bool(ift_items) and all((x["status"] or "").lower() in settings["ift_statuses"] for x in ift_items)

        has_dist_attachment = _has_distribution_attachment(story_issue, settings["distribution_keywords"])
        dist_issue_ok = bool(dist_items) and all(
            (x["status"] or "").lower() in settings["distribution_statuses"] for x in dist_items
        )
        distribution_ok = has_dist_attachment or dist_issue_ok

        story_ok = co_ok and ift_ok and distribution_ok
        if not story_ok:
            failed += 1

        story_results.append({
            "story_key": story_key,
            "story_summary": _issue_summary(story_issue),
            "co_items": co_items,
            "ift_items": ift_items,
            "distribution_items": dist_items,
            "distribution_attachment_found": has_dist_attachment,
            "co_ok": co_ok,
            "ift_ok": ift_ok,
            "distribution_ok": distribution_ok,
            "ok": story_ok,
        })

    passed = len(story_results) - failed
    return {
        "success": True,
        "release_key": release_key,
        "total_stories": len(story_results),
        "passed_stories": passed,
        "failed_stories": failed,
        "story_results": story_results,
        "settings": settings,
    }


def trigger_rqg_button(jira_service, release_key: str, button_name: Optional[str] = None) -> Dict:
    """Нажимает кнопку/переход RQG в Jira на релизной задаче."""
    transition_name = (button_name or os.getenv("RQG_TRANSITION_NAME", "RQG")).strip()
    ok, message = jira_service.transition_issue(release_key, transition_name)
    return {
        "success": ok,
        "release_key": release_key,
        "transition_name": transition_name,
        "message": message,
    }


def format_rqg_report(result: Dict) -> str:
    if not result.get("success"):
        return f"❌ RQG: {result.get('message', 'Неизвестная ошибка')}"

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append(f"🛡 RQG ОТЧЕТ: {result['release_key']}")
    lines.append("=" * 80)
    lines.append(f"Story проверено: {result['total_stories']}")
    lines.append(f"✅ Пройдено: {result['passed_stories']}")
    lines.append(f"❌ Не пройдено: {result['failed_stories']}")
    lines.append("")

    if result["total_stories"] == 0:
        lines.append("⚠️ В релизе не найдено Story для RQG-проверки.")
        return "\n".join(lines)

    for story in result["story_results"]:
        mark = "✅" if story["ok"] else "❌"
        lines.append(f"{mark} {story['story_key']} — {_short(story['story_summary'])}")
        lines.append(f"   ЦО: {'OK' if story['co_ok'] else 'FAIL'} | ИФТ: {'OK' if story['ift_ok'] else 'FAIL'} | Дистрибутив: {'OK' if story['distribution_ok'] else 'FAIL'}")
        if not story["co_ok"]:
            lines.append("   - ЦО: не найдено или статус не соответствует")
        if not story["ift_ok"]:
            lines.append("   - ИФТ: не найдено или статус не соответствует")
        if not story["distribution_ok"]:
            lines.append("   - Дистрибутив: не найдено вложение/связанный элемент с допустимым статусом")
        lines.append("")

    lines.append("Примечание:")
    lines.append("- Правила RQG можно настроить через .env (RQG_*).")
    lines.append("=" * 80)
    return "\n".join(lines)


def _short(text: str, limit: int = 70) -> str:
    if len(text or "") <= limit:
        return text or ""
    return f"{text[:limit - 3]}..."


def run_rqg_check(
    jira_service,
    release_key: str,
    max_depth: int = 2,
    trigger_button: bool = True,
    button_name: Optional[str] = None,
) -> str:
    lines: List[str] = []
    if trigger_button:
        trigger_result = trigger_rqg_button(jira_service, release_key, button_name=button_name)
        if trigger_result["success"]:
            lines.append(
                f"✅ Jira-кнопка RQG нажата: {trigger_result['transition_name']} "
                f"({trigger_result['release_key']})"
            )
        else:
            lines.append(
                f"⚠️ Не удалось нажать Jira-кнопку RQG "
                f"('{trigger_result['transition_name']}'): {trigger_result['message']}"
            )
        lines.append("")

    result = analyze_rqg_for_release(jira_service=jira_service, release_key=release_key, max_depth=max_depth)
    lines.append(format_rqg_report(result))
    return "\n".join(lines)
