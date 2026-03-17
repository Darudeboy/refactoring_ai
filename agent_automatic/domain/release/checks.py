from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_automatic.domain.release.models import ManualCheck, ReleaseProfile


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _contains_any(text: str, keywords: list[str]) -> bool:
    lowered = _norm(text)
    return any(_norm(word) in lowered for word in (keywords or []))


def _status_exact_in(status: str, allowed_statuses: list[str]) -> bool:
    status_norm = _norm(status)
    allowed = {_norm(item) for item in (allowed_statuses or [])}
    return status_norm in allowed


@dataclass(frozen=True, slots=True)
class ManualCheckStatus:
    id: str
    title: str
    status: str  # manual | optional_missing
    message: str


def evaluate_manual_checks(release_issue: dict, related_issues: list[dict], profile: ReleaseProfile) -> list[ManualCheckStatus]:
    status_by_keyword: list[dict[str, str]] = []
    all_issues = [release_issue] + (related_issues or [])
    for issue in all_issues:
        fields = issue.get("fields", {}) or {}
        for sub in fields.get("subtasks", []) or []:
            sub_summary = str(sub.get("fields", {}).get("summary", ""))
            sub_status = str(sub.get("fields", {}).get("status", {}).get("name", "Unknown"))
            status_by_keyword.append({"summary": sub_summary, "status": sub_status, "key": str(sub.get("key", ""))})

    pending: list[ManualCheckStatus] = []
    for check in profile.manual_checks:
        pending.extend(_evaluate_manual_check(status_by_keyword, check))
    return pending


def _evaluate_manual_check(status_by_keyword: list[dict[str, str]], check: ManualCheck) -> list[ManualCheckStatus]:
    keywords = check.keywords
    required_statuses = check.required_statuses

    if not keywords:
        return [
            ManualCheckStatus(
                id=check.id,
                title=check.title,
                status="manual",
                message="Требуется ручное подтверждение.",
            )
        ]

    matched = [item for item in status_by_keyword if _contains_any(item.get("summary", ""), keywords)]
    if not matched:
        return [
            ManualCheckStatus(
                id=check.id,
                title=check.title,
                status="optional_missing",
                message="Подзадача не найдена (проверь, требуется ли для проекта).",
            )
        ]

    bad = [item for item in matched if not _status_exact_in(item.get("status", ""), required_statuses)]
    if bad:
        return [
            ManualCheckStatus(
                id=check.id,
                title=check.title,
                status="manual",
                message=f"Есть незакрытые подзадачи: {', '.join(x.get('key') or x.get('summary', '') for x in bad)}",
            )
        ]
    return []


def derive_business_project(release_issue: dict, related_issues: list[dict]) -> str:
    for issue in related_issues or []:
        issue_type = str((issue.get("fields", {}) or {}).get("issuetype", {}).get("name", "")).lower()
        if issue_type in ("story", "bug", "история", "дефект"):
            project_key = str((issue.get("fields", {}) or {}).get("project", {}).get("key", "")).strip().upper()
            if project_key:
                return project_key
    return str((release_issue.get("fields", {}) or {}).get("project", {}).get("key", "")).strip().upper()


def distribution_from_related_issues(related_issues: list[dict]) -> dict[str, bool]:
    link_present = False
    registered = False
    dist_markers = ("дистриб", "distribution", "distrib", "release-notes", "install")
    approved_markers = ("утвержден", "approved", "согласован", "выполн", "закры")

    for issue in related_issues or []:
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


def comment_text(comment: dict) -> str:
    body: Any = comment.get("body", "")
    if isinstance(body, str):
        return body
    return str(body)


def extract_rqg_comment_signals(comments: list[dict]) -> dict[str, bool]:
    text_blob = "\n".join(comment_text(c) for c in (comments or [])).lower()
    return {
        "rqg_success": ("проверки rqg успешно выполнены" in text_blob) or ("rqg" in text_blob and "успеш" in text_blob),
        "testing_completed": ("запланированный объём тестирования: выполнен" in text_blob)
        or ("запланированный объем тестирования: выполнен" in text_blob),
        "no_critical_bugs": ("открытые блокирующие и критичные дефекты: нет" in text_blob)
        or ("критичные дефекты: нет" in text_blob),
        "recommended_to_psi": ("рекомендации по переводу на пси: рекомендован" in text_blob)
        or ("рекомендован" in text_blob and "пси" in text_blob),
    }

