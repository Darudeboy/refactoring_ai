from __future__ import annotations

from enum import StrEnum


class CommandIntent(StrEnum):
    get_jira_status = "get_jira_status"
    create_deploy_plan = "create_deploy_plan"
    create_business_requirements = "create_business_requirements"
    check_lead_time = "check_lead_time"
    check_rqg = "check_rqg"
    update_architecture_status = "update_architecture_status"
    move_release_status = "move_release_status"
    run_release_pipeline = "run_release_pipeline"
    check_release_tasks_pr_status = "check_release_tasks_pr_status"
    link_tasks_to_release = "link_tasks_to_release"
    link_issues_by_fix_version = "link_issues_by_fix_version"
    start_release_guided_cycle = "start_release_guided_cycle"
    run_next_release_step = "run_next_release_step"
    confirm_manual_check = "confirm_manual_check"
    move_release_if_ready = "move_release_if_ready"

