from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class JiraSettings:
    url: str
    token: str
    verify_ssl: bool = False


@dataclass(frozen=True, slots=True)
class ConfluenceSettings:
    url: str
    token: str | None
    space_key: str
    parent_page_title: str
    template_page_id: str | None
    verify_ssl: bool = False


@dataclass(frozen=True, slots=True)
class AppSettings:
    workspace_root: Path
    configs_dir: Path
    release_profiles_dir: Path
    prompts_dir: Path

    jira: JiraSettings
    confluence: ConfluenceSettings

    release_flow_hotfix_projects: set[str]


def load_settings(workspace_root: str | Path) -> AppSettings:
    root = Path(workspace_root).resolve()
    configs_dir = root / "agent_automatic" / "configs"

    jira_url = os.getenv("JIRA_URL", "https://jira.sberbank.ru")
    jira_token = os.getenv("JIRA_TOKEN", "")
    verify_ssl = os.getenv("JIRA_VERIFY_SSL", "false").strip().lower() in {"1", "true", "yes", "y", "да"}

    confluence_url = os.getenv("CONFLUENCE_URL", "https://confluence.sberbank.ru")
    confluence_token = os.getenv("CONFLUENCE_TOKEN")
    confluence_space_key = os.getenv("CONFLUENCE_SPACE_KEY", "HRTECH")
    confluence_parent_page_title = os.getenv("CONFLUENCE_PARENT_PAGE_TITLE", "deploy plan 2k")
    confluence_template_page_id = os.getenv("CONFLUENCE_TEMPLATE_PAGE_ID")

    hotfix_projects_raw = os.getenv("RELEASE_FLOW_HOTFIX_PROJECTS", "HOTFIX,HF")
    hotfix_projects = {p.strip().upper() for p in hotfix_projects_raw.split(",") if p.strip()}

    return AppSettings(
        workspace_root=root,
        configs_dir=configs_dir,
        release_profiles_dir=configs_dir / "release_profiles",
        prompts_dir=configs_dir / "prompts",
        jira=JiraSettings(url=jira_url, token=jira_token, verify_ssl=verify_ssl),
        confluence=ConfluenceSettings(
            url=confluence_url,
            token=confluence_token,
            space_key=confluence_space_key,
            parent_page_title=confluence_parent_page_title,
            template_page_id=confluence_template_page_id,
            verify_ssl=False,
        ),
        release_flow_hotfix_projects=hotfix_projects,
    )

