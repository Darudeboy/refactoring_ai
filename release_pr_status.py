from __future__ import annotations

from typing import Callable, Dict, List, Tuple


def _is_story_or_bug(issue_type: str) -> bool:
    normalized = (issue_type or "").strip().lower()
    return normalized in {"story", "bug", "история", "дефект"}


def _looks_like_pr(text: str) -> bool:
    value = (text or "").lower()
    markers = (
        "pull request",
        "pull-request",
        "merge request",
        "/pull/",
        "/pulls/",
        "/merge_requests/",
        "bitbucket.org",
        "github.com",
        "gitlab",
    )
    return any(marker in value for marker in markers)


def _detect_pr_status(*texts: str) -> str:
    joined = " ".join(texts).lower()
    if any(token in joined for token in ("merged", "merge", "влит", "смёржен", "closed", "закрыт")):
        return "Merged"
    if any(token in joined for token in ("open", "opened", "открыт", "active")):
        return "Open"
    return "Unknown"


def _extract_prs_from_issue_links(issue: dict) -> List[Dict[str, str]]:
    prs: List[Dict[str, str]] = []
    for link in issue.get("fields", {}).get("issuelinks", []) or []:
        link_type = link.get("type", {}) or {}
        type_blob = " ".join(
            [
                str(link_type.get("name", "")),
                str(link_type.get("inward", "")),
                str(link_type.get("outward", "")),
            ]
        )
        if not _looks_like_pr(type_blob):
            continue

        rel = link.get("outwardIssue") or link.get("inwardIssue") or {}
        rel_fields = rel.get("fields", {}) if isinstance(rel, dict) else {}
        rel_key = rel.get("key", "PR-link")
        rel_summary = rel_fields.get("summary", "")
        rel_status = rel_fields.get("status", {}).get("name", "")
        status = _detect_pr_status(rel_status, type_blob, rel_summary)
        prs.append(
            {
                "id": rel_key,
                "title": rel_summary or str(link_type.get("name", "PR")),
                "status": status,
                "source": "issuelinks",
            }
        )
    return prs


def _extract_prs_from_remote_links(remote_links: List[dict]) -> List[Dict[str, str]]:
    prs: List[Dict[str, str]] = []
    for item in remote_links or []:
        obj = item.get("object", {}) or {}
        app = item.get("application", {}) or {}
        url = str(obj.get("url", ""))
        title = str(obj.get("title", "") or obj.get("summary", ""))
        app_name = str(app.get("name", ""))
        if not _looks_like_pr(" ".join([url, title, app_name])):
            continue

        status_payload = item.get("status", {}) or {}
        obj_status = obj.get("status", {}) or {}
        text_for_status = " ".join(
            [
                str(status_payload.get("resolved", "")),
                str(status_payload.get("icon", {}).get("title", "")),
                str(obj_status.get("resolved", "")),
                str(obj_status.get("icon", {}).get("title", "")),
                title,
            ]
        )
        status = _detect_pr_status(text_for_status)
        prs.append(
            {
                "id": url or title or "remote-pr",
                "title": title or url,
                "status": status,
                "url": url,
                "source": "remotelink",
            }
        )
    return prs


def _deduplicate_prs(prs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    deduped: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for pr in prs:
        key = (pr.get("id", ""), pr.get("source", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pr)
    return deduped


def collect_release_tasks_pr_status(
    jira_service,
    release_key: str,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict:
    """Собирает статус Story/Bug и связанный статус Pull Request для релиза."""
    release = jira_service.get_issue_details(release_key)
    if not release:
        return {"success": False, "message": f"Релиз {release_key} не найден."}

    linked_keys = jira_service.get_linked_issues(release_key)
    if not linked_keys:
        return {
            "success": True,
            "release_key": release_key,
            "items": [],
            "summary": {
                "total_tasks": 0,
                "with_pr": 0,
                "without_pr": 0,
                "merged_pr": 0,
                "open_pr": 0,
                "unknown_pr": 0,
            },
        }

    if progress_callback:
        progress_callback(0.05, f"Найдено связанных задач: {len(linked_keys)}")

    items: List[dict] = []
    total = len(linked_keys)

    for index, issue_key in enumerate(linked_keys, start=1):
        issue = jira_service.get_issue_details(issue_key)
        if not issue:
            continue

        fields = issue.get("fields", {}) or {}
        issue_type = fields.get("issuetype", {}).get("name", "")
        if not _is_story_or_bug(issue_type):
            if progress_callback:
                progress_callback(
                    index / total,
                    f"Пропускаю {issue_key}: тип {issue_type or 'Unknown'}",
                )
            continue

        status = fields.get("status", {}).get("name", "Unknown")
        summary = fields.get("summary", "")
        remote_links = jira_service.get_issue_remote_links(issue_key)

        prs = _extract_prs_from_issue_links(issue)
        prs.extend(_extract_prs_from_remote_links(remote_links))
        prs = _deduplicate_prs(prs)

        items.append(
            {
                "issue_key": issue_key,
                "issue_type": issue_type,
                "summary": summary,
                "status": status,
                "prs": prs,
            }
        )

        if progress_callback:
            progress_callback(index / total, f"Обработана задача {issue_key}")

    merged_pr = 0
    open_pr = 0
    unknown_pr = 0
    with_pr = 0

    for item in items:
        prs = item.get("prs", [])
        if prs:
            with_pr += 1
        for pr in prs:
            pr_status = pr.get("status", "Unknown")
            if pr_status == "Merged":
                merged_pr += 1
            elif pr_status == "Open":
                open_pr += 1
            else:
                unknown_pr += 1

    summary = {
        "total_tasks": len(items),
        "with_pr": with_pr,
        "without_pr": max(len(items) - with_pr, 0),
        "merged_pr": merged_pr,
        "open_pr": open_pr,
        "unknown_pr": unknown_pr,
    }
    return {"success": True, "release_key": release_key, "items": items, "summary": summary}


def format_release_tasks_pr_report(report: dict) -> str:
    """Форматирует сводный отчет по статусу задач и PR."""
    if not report.get("success"):
        return f"❌ Ошибка: {report.get('message', 'Неизвестная ошибка')}"

    summary = report.get("summary", {})
    lines: List[str] = []
    lines.append("=" * 80)
    lines.append(f"🔎 ОТЧЕТ ПО ЗАДАЧАМ И PR: {report.get('release_key', '-')}")
    lines.append("=" * 80)
    lines.append(f"Story/Bug задач: {summary.get('total_tasks', 0)}")
    lines.append(f"С PR: {summary.get('with_pr', 0)} | Без PR: {summary.get('without_pr', 0)}")
    lines.append(
        "PR статусы: "
        f"Merged={summary.get('merged_pr', 0)}, "
        f"Open={summary.get('open_pr', 0)}, "
        f"Unknown={summary.get('unknown_pr', 0)}"
    )
    lines.append("")

    items = report.get("items", [])
    if not items:
        lines.append("⚠️ В релизе не найдено Story/Bug задач.")
        lines.append("=" * 80)
        return "\n".join(lines)

    for item in items:
        lines.append(
            f"• {item['issue_key']} [{item.get('issue_type', 'Task')}] "
            f"— статус: {item.get('status', 'Unknown')}"
        )
        if item.get("summary"):
            lines.append(f"  {item['summary']}")

        prs = item.get("prs", [])
        if not prs:
            lines.append("  PR: не найдены")
        else:
            for pr in prs:
                title = (pr.get("title") or pr.get("id") or "PR").strip()
                status = pr.get("status", "Unknown")
                lines.append(f"  PR [{status}]: {title}")
        lines.append("")

    lines.append("=" * 80)
    return "\n".join(lines)
