from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_automatic.domain.common.enums import CommandIntent


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    intent: CommandIntent
    raw_text: str
    release_key: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConversationState:
    last_release_key: str | None = None
    pending_intent: str | None = None
    pending_slots: dict[str, str] = field(default_factory=dict)
    last_project_key: str | None = None

