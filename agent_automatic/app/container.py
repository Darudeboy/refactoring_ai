from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_automatic.app.settings import AppSettings
from agent_automatic.application.services.conversation_service import ConversationService
from agent_automatic.application.services.release_orchestrator import ReleaseOrchestrator
from agent_automatic.application.services.release_workflow_engine import ReleaseWorkflowEngine
from agent_automatic.application.use_cases.move_release import MoveReleaseUseCase
from agent_automatic.application.use_cases.run_next_step import RunNextStepUseCase
from agent_automatic.domain.commands.parser import CommandParser
from agent_automatic.domain.commands.router import CommandRouter
from agent_automatic.infrastructure.jira.client import JiraClient
from agent_automatic.infrastructure.jira.service import JiraService
from agent_automatic.infrastructure.jira.transition_resolver import TransitionResolver
from agent_automatic.infrastructure.storage.session_repo import SessionRepo
from agent_automatic.interfaces.ui.controllers import ChatController, ControllerDeps


@dataclass(frozen=True, slots=True)
class Container:
    settings: AppSettings
    chat_controller: ChatController


def build_container(settings: AppSettings) -> Container:
    jira_client = JiraClient(
        base_url=settings.jira.url,
        token=settings.jira.token,
        verify_ssl=settings.jira.verify_ssl,
    )
    jira_service = JiraService(jira_client)

    transition_resolver = TransitionResolver()
    engine = ReleaseWorkflowEngine(
        jira_service=jira_service,
        transition_resolver=transition_resolver,
        release_profiles_dir=settings.release_profiles_dir,
        hotfix_projects=settings.release_flow_hotfix_projects,
    )

    orchestrator = ReleaseOrchestrator(
        jira=jira_service,
        engine=engine,
        release_profiles_dir=settings.release_profiles_dir,
        hotfix_projects=settings.release_flow_hotfix_projects,
    )

    run_next_step = RunNextStepUseCase(jira=jira_service, engine=engine)
    move_release = MoveReleaseUseCase(jira=jira_service)

    sessions = SessionRepo(str(Path(settings.workspace_root) / ".agent_automatic" / "sessions.json"))
    conversation = ConversationService(sessions)

    deps = ControllerDeps(
        parser=CommandParser(),
        router=CommandRouter(),
        conversation=conversation,
        run_next_step=run_next_step,
        move_release=move_release,
        orchestrator=orchestrator,
    )
    controller = ChatController(deps)

    return Container(settings=settings, chat_controller=controller)

