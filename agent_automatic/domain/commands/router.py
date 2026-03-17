from __future__ import annotations

from dataclasses import dataclass

from agent_automatic.domain.commands.models import ParsedCommand
from agent_automatic.domain.common.errors import InvalidCommand
from agent_automatic.domain.common.enums import CommandIntent


@dataclass(frozen=True, slots=True)
class RoutedCommand:
    intent: CommandIntent
    handler_key: str
    command: ParsedCommand


class CommandRouter:
    def route(self, cmd: ParsedCommand) -> RoutedCommand:
        if not cmd or not cmd.intent:
            raise InvalidCommand("Empty command")
        return RoutedCommand(intent=cmd.intent, handler_key=str(cmd.intent.value), command=cmd)

