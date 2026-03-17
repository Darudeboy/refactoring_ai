from __future__ import annotations

from agent_automatic.domain.commands.models import ConversationState, ParsedCommand
from agent_automatic.domain.common.enums import CommandIntent
from agent_automatic.infrastructure.storage.session_repo import SessionRepo


class ConversationService:
    def __init__(self, sessions: SessionRepo):
        self.sessions = sessions

    def apply(self, *, conversation_id: str, cmd: ParsedCommand | None) -> ParsedCommand | None:
        """
        Takes current user input command; if it is a continuation message (like project key after a pending create_bt),
        completes the pending command and returns the effective ParsedCommand to execute.
        """
        if not cmd:
            return None
        state = self.sessions.get(conversation_id)

        if cmd.intent == CommandIntent.get_jira_status and "token" in cmd.slots and state.pending_intent:
            token = str(cmd.slots.get("token") or "").strip().upper()
            if token:
                if state.pending_intent == CommandIntent.create_business_requirements.value and state.last_release_key:
                    effective = ParsedCommand(
                        intent=CommandIntent.create_business_requirements,
                        raw_text=cmd.raw_text,
                        release_key=state.last_release_key,
                        slots={"project_key": token},
                    )
                    state.pending_intent = None
                    state.pending_slots = {}
                    state.last_project_key = token
                    self.sessions.save(conversation_id, state)
                    return effective

        # Set last_release_key on any release-scoped command
        if cmd.release_key:
            state.last_release_key = cmd.release_key

        if cmd.intent == CommandIntent.create_business_requirements:
            project_key = str(cmd.slots.get("project_key") or "").strip().upper()
            if not project_key:
                state.pending_intent = CommandIntent.create_business_requirements.value
                state.pending_slots = {}
                self.sessions.save(conversation_id, state)
                return ParsedCommand(
                    intent=CommandIntent.create_business_requirements,
                    raw_text=cmd.raw_text,
                    release_key=cmd.release_key,
                    slots={"project_key": ""},
                )

        self.sessions.save(conversation_id, state)
        return cmd

