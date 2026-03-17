from __future__ import annotations

from dataclasses import dataclass

from agent_automatic.application.services.release_profile_loader import get_release_profile
from agent_automatic.domain.common.errors import ReleaseNotFound
from agent_automatic.domain.release.transitions import ReleaseWorkflowPlan, ResolvedTransition
from agent_automatic.domain.release.workflow import is_terminal, next_status
from agent_automatic.infrastructure.jira.mappers import issue_project_key, issue_status
from agent_automatic.infrastructure.jira.service import JiraService
from agent_automatic.infrastructure.jira.transition_resolver import TransitionResolver


@dataclass(frozen=True, slots=True)
class BuildPlanInput:
    release_key: str
    requested_profile: str = "auto"


class ReleaseWorkflowEngine:
    def __init__(
        self,
        *,
        jira_service: JiraService,
        transition_resolver: TransitionResolver,
        release_profiles_dir,
        hotfix_projects: set[str],
    ):
        self.jira = jira_service
        self.resolver = transition_resolver
        self.release_profiles_dir = release_profiles_dir
        self.hotfix_projects = hotfix_projects

    def build_plan(self, inp: BuildPlanInput) -> ReleaseWorkflowPlan:
        safe_key = (inp.release_key or "").strip().upper()
        if not safe_key:
            raise ReleaseNotFound("release_key is empty")

        release = self.jira.get_issue_details(safe_key, expand="issuelinks,renderedFields,names")
        if not release:
            raise ReleaseNotFound(f"Релиз {safe_key} не найден.")

        current = issue_status(release)
        project_key = issue_project_key(release)

        profile = get_release_profile(
            self.release_profiles_dir,
            project_key=project_key,
            requested_profile=inp.requested_profile,
            hotfix_projects=self.hotfix_projects,
        )

        explain: list[str] = []
        explain.append(f"current_status={current}")
        explain.append(f"profile={profile.name}")

        if is_terminal(current, profile.terminal_statuses):
            explain.append("terminal_status=true")
            return ReleaseWorkflowPlan(
                release_key=safe_key,
                current_status=current,
                expected_next_status=None,
                resolved_transition=ResolvedTransition(id=None, name=None),
                profile_name=profile.name,
                explain=explain,
            )

        expected = next_status(current, profile.workflow_order)
        explain.append(f"expected_next_status={expected or '-'}")

        if not expected:
            return ReleaseWorkflowPlan(
                release_key=safe_key,
                current_status=current,
                expected_next_status=None,
                resolved_transition=ResolvedTransition(id=None, name=None),
                profile_name=profile.name,
                explain=explain,
            )

        available = self.jira.get_available_transitions(safe_key)
        explain.append(f"available_transitions={len(available)}")

        preferred_id = profile.transition_ids.get(expected)
        if preferred_id:
            explain.append(f"preferred_transition_id={preferred_id}")

        resolved = self.resolver.resolve(
            expected_status=expected,
            available_transitions=available,
            aliases=profile.transition_aliases,
            preferred_transition_id=preferred_id,
        )
        explain.append(f"resolved_transition={resolved.name or '-'} ({resolved.id or '-'})")

        return ReleaseWorkflowPlan(
            release_key=safe_key,
            current_status=current,
            expected_next_status=expected,
            resolved_transition=resolved,
            profile_name=profile.name,
            explain=explain,
        )

