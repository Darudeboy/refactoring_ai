from __future__ import annotations

from dataclasses import asdict

from agent_automatic.application.dto.release_dto import GuidedCycleReport
from agent_automatic.application.services.release_profile_loader import get_release_profile
from agent_automatic.application.services.release_workflow_engine import BuildPlanInput, ReleaseWorkflowEngine
from agent_automatic.domain.release.checks import derive_business_project, evaluate_manual_checks
from agent_automatic.domain.release.policies import evaluate_release_gates_domain
from agent_automatic.domain.release.workflow import is_terminal
from agent_automatic.infrastructure.jira.mappers import issue_project_key, issue_status
from agent_automatic.infrastructure.jira.service import JiraService


class ReleaseOrchestrator:
    def __init__(
        self,
        *,
        jira: JiraService,
        engine: ReleaseWorkflowEngine,
        release_profiles_dir,
        hotfix_projects: set[str],
    ):
        self.jira = jira
        self.engine = engine
        self.release_profiles_dir = release_profiles_dir
        self.hotfix_projects = hotfix_projects

    def evaluate_release(
        self,
        *,
        release_key: str,
        profile_name: str = "auto",
        manual_confirmations: dict[str, bool] | None = None,
    ) -> dict:
        safe = (release_key or "").strip().upper()
        release = self.jira.get_issue_details(safe, expand="issuelinks,renderedFields,names")
        if not release:
            return {"success": False, "message": f"Релиз {safe} не найден."}
        release.setdefault("fields", {})
        release.setdefault("renderedFields", {})

        # Доп. источник правды для IFT/NT/DT сигналов.
        sber_test_html = self.jira.get_sber_test_report(safe)
        if sber_test_html:
            release["fields"]["customfield_sber_test_html"] = sber_test_html
            release["renderedFields"]["customfield_sber_test_html"] = sber_test_html

        release_project_key = issue_project_key(release)
        profile = get_release_profile(
            self.release_profiles_dir,
            project_key=release_project_key,
            requested_profile=profile_name,
            hotfix_projects=self.hotfix_projects,
        )

        linked_keys = self.jira.get_linked_issues(safe)
        related_issues = [self.jira.get_issue_details(k) for k in linked_keys]
        related_issues = [x for x in related_issues if x]

        project_key = derive_business_project(release, related_issues)

        field_name_map = self.jira.get_field_name_map()
        dev_summary = self.jira.get_dev_status_summary(safe)
        qgm_ok, qgm_message, qgm_payload = self.jira.get_qgm_status(safe)
        comments = self.jira.get_issue_comments(safe)

        gate_payload = evaluate_release_gates_domain(
            release_issue=release,
            related_issues=related_issues,
            profile=profile,
            field_name_map=field_name_map,
            dev_summary=dev_summary,
            qgm_ok=qgm_ok,
            qgm_message=qgm_message,
            qgm_payload=qgm_payload or {},
            comments=comments,
        )

        manual_raw = evaluate_manual_checks(release, related_issues, profile)
        manual_pending = [asdict(x) for x in manual_raw if x.status != "optional_missing"]
        manual_optional = [asdict(x) for x in manual_raw if x.status == "optional_missing"]

        confirmations = manual_confirmations or {}
        manual_done = []
        still_pending = []
        for item in manual_pending:
            check_id = item.get("id")
            if confirmations.get(check_id) is True:
                manual_done.append(item)
            else:
                still_pending.append(item)
        manual_pending = still_pending

        plan = self.engine.build_plan(BuildPlanInput(release_key=safe, requested_profile=profile_name))
        current_status = issue_status(release)
        terminal = is_terminal(current_status, profile.terminal_statuses)

        next_status = plan.expected_next_status
        next_transition_id = plan.resolved_transition.id or profile.transition_ids.get(next_status or "")

        auto_failed = gate_payload.get("auto_failed", [])
        ready_for_transition = len(auto_failed) == 0 and len(manual_pending) == 0 and bool(next_status)
        cycle_completed = terminal and len(auto_failed) == 0

        report = GuidedCycleReport(
            release_key=safe,
            project_key=project_key,
            profile_name=profile.name,
            current_stage=current_status,
            next_allowed_transition=next_status,
            next_allowed_transition_id=next_transition_id,
            is_terminal_status=terminal,
            cycle_completed=cycle_completed,
            ready_for_transition=ready_for_transition,
            auto_passed=gate_payload.get("auto_passed", []),
            auto_failed=auto_failed,
            manual_pending=manual_pending,
            manual_optional=manual_optional,
            manual_done=manual_done,
            story_results=gate_payload.get("story_results", []),
            bug_results=gate_payload.get("bug_results", []),
            rqg_qgm=gate_payload.get("rqg_qgm", {}),
        )

        return {"success": True, **asdict(report)}

