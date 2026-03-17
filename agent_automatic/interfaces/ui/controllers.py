from __future__ import annotations

from dataclasses import dataclass

from agent_automatic.application.services.conversation_service import ConversationService
from agent_automatic.application.services.release_orchestrator import ReleaseOrchestrator
from agent_automatic.application.use_cases.move_release import MoveReleaseUseCase
from agent_automatic.application.use_cases.run_next_step import RunNextStepUseCase
from agent_automatic.domain.commands.parser import CommandParser
from agent_automatic.domain.commands.router import CommandRouter
from agent_automatic.domain.common.enums import CommandIntent
from agent_automatic.domain.common.result import Result
from agent_automatic.interfaces.ui.presenters import present_guided_cycle, present_text_result


@dataclass(frozen=True, slots=True)
class ControllerDeps:
    parser: CommandParser
    router: CommandRouter
    conversation: ConversationService
    run_next_step: RunNextStepUseCase
    move_release: MoveReleaseUseCase
    orchestrator: ReleaseOrchestrator


class ChatController:
    def __init__(self, deps: ControllerDeps):
        self.deps = deps

    def handle_text(self, *, text: str, conversation_id: str = "default") -> str:
        cmd = self.deps.parser.parse(text)
        cmd = self.deps.conversation.apply(conversation_id=conversation_id, cmd=cmd)
        if not cmd:
            return ""

        if cmd.intent == CommandIntent.create_business_requirements and not str(cmd.slots.get("project_key") or "").strip():
            return "Какой проект? (например: SFILE)"

        _ = self.deps.router.route(cmd)

        if cmd.intent == CommandIntent.run_next_release_step:
            res = self.deps.run_next_step.execute(
                release_key=cmd.release_key or "",
                profile=str(cmd.slots.get("profile") or "auto"),
                dry_run=bool(cmd.slots.get("dry_run") or False),
            )
            return present_text_result(res)

        if cmd.intent == CommandIntent.move_release_status:
            res = self.deps.move_release.execute(
                release_key=cmd.release_key or "",
                target_status=str(cmd.slots.get("target_status") or ""),
            )
            return present_text_result(res)

        if cmd.intent == CommandIntent.start_release_guided_cycle:
            report = self.deps.orchestrator.evaluate_release(
                release_key=cmd.release_key or "",
                profile_name=str(cmd.slots.get("profile") or "auto"),
                manual_confirmations={},
            )
            return present_guided_cycle(report)

        return present_text_result(Result.failure(RuntimeError(f"Intent not implemented: {cmd.intent}")))

