from __future__ import annotations

from typing import Any

from agent_automatic.domain.common.result import Result


def present_text_result(result: Result[str]) -> str:
    if result.ok:
        return result.message or (result.value or "")
    return f"Ошибка: {result.message}"


def present_guided_cycle(report: dict[str, Any]) -> str:
    if not report.get("success"):
        return f"❌ {report.get('message', 'Ошибка оценки гейтов')}"

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append(f"🧭 GUIDED CYCLE: {report.get('release_key')}")
    lines.append("=" * 80)
    lines.append("Engine: agent-automatic")
    lines.append(f"Профиль: {report.get('profile_name')} | Проект: {report.get('project_key')}")
    lines.append(f"Текущий этап: {report.get('current_stage')}")
    lines.append(f"Следующий этап: {report.get('next_allowed_transition') or 'нет'}")
    if report.get("next_allowed_transition_id"):
        lines.append(f"Transition ID: {report.get('next_allowed_transition_id')}")
    lines.append("")

    lines.append(f"✅ Авто-гейты пройдены: {len(report.get('auto_passed', []))}")
    for gate in report.get("auto_passed", []):
        lines.append(f"  - {gate.get('title')}")
    lines.append(f"❌ Авто-гейты провалены: {len(report.get('auto_failed', []))}")
    for gate in report.get("auto_failed", []):
        lines.append(f"  - {gate.get('title')}: {gate.get('details')}")
    lines.append("")

    lines.append(f"📝 Ручные проверки pending: {len(report.get('manual_pending', []))}")
    for check in report.get("manual_pending", []):
        lines.append(f"  - {check.get('id')}: {check.get('message')}")
    if report.get("manual_optional"):
        lines.append(f"ℹ️ Опциональные проверки: {len(report.get('manual_optional', []))}")
        for check in report.get("manual_optional", []):
            lines.append(f"  - {check.get('id')}: {check.get('message')}")
    lines.append("")

    if report.get("cycle_completed"):
        lines.append("✅ Цикл завершен: релиз в финальном статусе.")
    elif report.get("ready_for_transition"):
        lines.append("🚀 Готов к переходу по workflow.")
    else:
        lines.append("⛔ Переход пока заблокирован (есть непройденные гейты или ручные проверки).")

    lines.append("=" * 80)
    return "\n".join(lines)

