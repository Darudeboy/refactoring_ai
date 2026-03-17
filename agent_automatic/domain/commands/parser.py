from __future__ import annotations

import re

from agent_automatic.domain.commands.models import ParsedCommand
from agent_automatic.domain.common.enums import CommandIntent


class CommandParser:
    RELEASE_RE = re.compile(r"(HRPRELEASE-\d+)", re.IGNORECASE)

    def parse(self, text: str) -> ParsedCommand | None:
        raw = (text or "").strip()
        if not raw:
            return None
        lowered = raw.lower()

        release_match = self.RELEASE_RE.search(raw)
        release_key = release_match.group(1).upper() if release_match else None

        # Pending-only answers like "SFILE" will be handled by ConversationService.
        if re.fullmatch(r"\s*[A-Z][A-Z0-9_]{1,15}\s*", raw, re.IGNORECASE) and not release_key:
            return ParsedCommand(intent=CommandIntent.get_jira_status, raw_text=raw, release_key=None, slots={"token": raw.strip().upper()})

        if any(p in lowered for p in ("следующий шаг", "next step", "продолжи релиз", "продолжай цикл", "двигай")) and release_key:
            dry_run = "dry-run" in lowered or "dry run" in lowered or "тестовый прогон" in lowered
            return ParsedCommand(
                intent=CommandIntent.run_next_release_step,
                raw_text=raw,
                release_key=release_key,
                slots={"dry_run": dry_run},
            )

        if any(p in lowered for p in ("guided cycle", "полный цикл релиза", "запусти цикл релиза", "пошаговый релиз")) and release_key:
            profile = "hotfix" if "hotfix" in lowered else "auto"
            dry_run = "dry-run" in lowered or "dry run" in lowered or "тестовый прогон" in lowered
            return ParsedCommand(
                intent=CommandIntent.start_release_guided_cycle,
                raw_text=raw,
                release_key=release_key,
                slots={"profile": profile, "dry_run": dry_run},
            )

        if any(p in lowered for p in ("проверь rqg", "rqg провер", "запусти rqg", "run rqg")) and release_key:
            depth_match = re.search(r"(?:max_depth|глубин\w*)\s*[:=]?\s*(\d+)", raw, re.IGNORECASE)
            max_depth = int(depth_match.group(1)) if depth_match else 2
            return ParsedCommand(
                intent=CommandIntent.check_rqg,
                raw_text=raw,
                release_key=release_key,
                slots={"max_depth": max_depth, "trigger_button": True},
            )

        if any(p in lowered for p in ("проверь задачи и pr", "задачи и pr", "статус pr", "pr статус", "check pr", "check tasks and pr")) and release_key:
            return ParsedCommand(
                intent=CommandIntent.check_release_tasks_pr_status,
                raw_text=raw,
                release_key=release_key,
                slots={},
            )

        if any(p in lowered for p in ("собери задачи", "привяжи задачи", "привязать задачи", "линкуй задачи", "link tasks")) and release_key:
            fix_version = self._extract_fix_version(raw)
            if fix_version:
                return ParsedCommand(
                    intent=CommandIntent.link_issues_by_fix_version,
                    raw_text=raw,
                    release_key=release_key,
                    slots={"fix_version": fix_version},
                )

        if any(p in lowered for p in ("полный пайплайн", "запусти пайплайн", "run pipeline", "release pipeline")) and release_key:
            project_key = self._extract_project_key(raw)
            create_bt = self._extract_bool_slot(raw, "create_bt", default=False)
            create_deploy = self._extract_bool_slot(raw, "create_deploy", default=False)
            return ParsedCommand(
                intent=CommandIntent.run_release_pipeline,
                raw_text=raw,
                release_key=release_key,
                slots={
                    "project_key": project_key,
                    "create_bt": create_bt,
                    "create_deploy": create_deploy,
                },
            )

        if any(p in lowered for p in ("сделай бт", "бизнес-треб", "business requirement", "create bt")) and release_key:
            project_key = self._extract_project_key(raw)
            return ParsedCommand(
                intent=CommandIntent.create_business_requirements,
                raw_text=raw,
                release_key=release_key,
                slots={"project_key": project_key},
            )

        if any(p in lowered for p in ("переведи", "move", "в статус")) and release_key:
            target_status = self._extract_target_status(raw)
            if target_status:
                return ParsedCommand(
                    intent=CommandIntent.move_release_status,
                    raw_text=raw,
                    release_key=release_key,
                    slots={"target_status": target_status},
                )

        return None

    def _extract_project_key(self, text: str) -> str:
        match = re.search(r"(?:project|проект)\s*[:=]?\s*([A-Z][A-Z0-9_]{1,15})\b", text, re.IGNORECASE)
        return match.group(1).upper() if match else ""

    def _extract_fix_version(self, payload: str) -> str:
        explicit = re.search(
            r"(?:верси\w*|fix\s*version|fixversion)\s*[:=]?\s*([A-Z0-9._\\-]+)",
            payload,
            re.IGNORECASE,
        )
        if explicit:
            candidate = explicit.group(1).strip()
            if re.fullmatch(r"HRPRELEASE-\d+", candidate, re.IGNORECASE):
                return ""
            if not re.fullmatch(r"[A-Z]+-\d+", candidate, re.IGNORECASE):
                return candidate
            return ""

        labeled = re.search(
            r"(?:project|проект)\s*[:=]?\s*[A-Z0-9_]+\s*[,;]?\s*(?:fix\s*version|fixversion|верси\w*)\s*[:=]?\s*([A-Z0-9._\\-]+)",
            payload,
            re.IGNORECASE,
        )
        if labeled:
            candidate = labeled.group(1).strip()
            if not re.fullmatch(r"[A-Z]+-\d+", candidate, re.IGNORECASE) and not re.fullmatch(
                r"HRPRELEASE-\d+",
                candidate,
                re.IGNORECASE,
            ):
                return candidate

        token_candidates = re.findall(r"\b([A-Z0-9][A-Z0-9._\\-]{4,})\b", payload, re.IGNORECASE)
        for token in token_candidates:
            upper = token.upper()
            if re.fullmatch(r"[A-Z]+-\d+", upper):
                continue
            if re.fullmatch(r"HRPRELEASE-\d+", upper):
                continue
            if upper in {"HRC", "HRM", "NEUROUI", "SFILE", "SEARCHCS", "NEURO", "HRPDEV"}:
                continue
            return token.strip()
        return ""

    def _extract_bool_slot(self, text: str, name: str, *, default: bool) -> bool:
        match = re.search(rf"{re.escape(name)}\s*[:=]\s*(true|false|1|0|yes|no|да|нет)", text, re.IGNORECASE)
        if not match:
            return default
        val = match.group(1).lower()
        return val in {"true", "1", "yes", "да"}

    def _extract_target_status(self, text: str) -> str:
        match = re.search(r"(?:в статус|to)\s*['\"]?([^'\"\\n]+?)['\"]?(?:\\.|$)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return ""

