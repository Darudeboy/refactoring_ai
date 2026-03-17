from __future__ import annotations


def norm(value: str) -> str:
    return (value or "").strip().lower()


def next_status(current_status: str, workflow_order: list[str]) -> str | None:
    normalized = [norm(x) for x in (workflow_order or [])]
    current = norm(current_status)
    if not current or current not in normalized:
        return None
    idx = normalized.index(current)
    if idx >= len(workflow_order) - 1:
        return None
    return workflow_order[idx + 1]


def is_terminal(current_status: str, terminal_statuses: list[str]) -> bool:
    current = norm(current_status)
    terminal = {norm(x) for x in (terminal_statuses or [])}
    return bool(current and current in terminal)

