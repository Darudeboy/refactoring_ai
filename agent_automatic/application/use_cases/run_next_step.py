from __future__ import annotations

from agent_automatic.application.services.release_workflow_engine import BuildPlanInput, ReleaseWorkflowEngine
from agent_automatic.domain.common.result import Result
from agent_automatic.infrastructure.jira.service import JiraService


class RunNextStepUseCase:
    def __init__(self, *, jira: JiraService, engine: ReleaseWorkflowEngine):
        self.jira = jira
        self.engine = engine

    def execute(self, *, release_key: str, profile: str = "auto", dry_run: bool = False) -> Result[str]:
        plan = self.engine.build_plan(BuildPlanInput(release_key=release_key, requested_profile=profile))
        if not plan.expected_next_status:
            return Result.success(message="Релиз уже в финальном статусе или следующий этап не определен.")

        if dry_run:
            msg = f"[DRY-RUN] Релиз {plan.release_key} готов к переходу в '{plan.expected_next_status}'"
            if plan.resolved_transition.id:
                msg += f" (transition id: {plan.resolved_transition.id})"
            msg += ". Фактический перевод не выполнен."
            return Result.success(message=msg)

        if plan.resolved_transition.id:
            ok, msg = self.jira.transition_issue_by_id(plan.release_key, plan.resolved_transition.id)
        else:
            ok, msg = self.jira.transition_issue(plan.release_key, plan.expected_next_status)

        if not ok:
            return Result.failure(RuntimeError(msg))
        return Result.success(message=f"Релиз {plan.release_key} переведен в '{plan.expected_next_status}'.")

