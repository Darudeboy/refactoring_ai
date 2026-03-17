from __future__ import annotations

from typing import Any


def issue_status(issue: dict) -> str:
    return str(((issue.get("fields") or {}).get("status") or {}).get("name") or "Unknown")


def issue_project_key(issue: dict) -> str:
    return str(((issue.get("fields") or {}).get("project") or {}).get("key") or "")


def issue_key(issue: dict) -> str:
    return str(issue.get("key") or "")


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}

