from __future__ import annotations

from agent_automatic.domain.common.result import Result
from agent_automatic.infrastructure.jira.service import JiraService


class MoveReleaseUseCase:
    def __init__(self, *, jira: JiraService):
        self.jira = jira

    def execute(self, *, release_key: str, target_status: str) -> Result[str]:
        ok, msg = self.jira.transition_issue(release_key, target_status)
        if not ok:
            return Result.failure(RuntimeError(msg))
        return Result.success(message=msg)

