from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_automatic.domain.commands.models import ParsedCommand
from agent_automatic.domain.common.result import Result


@dataclass(frozen=True, slots=True)
class HandlerContext:
    # carrier for cross-cutting concerns (state, user id, etc.)
    conversation_id: str = "default"


class CommandHandler(Protocol):
    def handle(self, cmd: ParsedCommand, ctx: HandlerContext) -> Result[str]: ...

