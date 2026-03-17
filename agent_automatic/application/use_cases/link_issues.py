"""Use case: привязка задач с заданным fixVersion к релизу."""
from __future__ import annotations

from agent_automatic.domain.common.result import Result
from agent_automatic.infrastructure.jira.service import JiraService


# Проекты для JQL по умолчанию (как в старом UI)
DEFAULT_LINK_PROJECTS = "HRM, HRC, NEUROUI, SFILE, SEARCHCS"


class LinkIssuesUseCase:
    def __init__(self, *, jira: JiraService):
        self.jira = jira

    def execute(
        self,
        *,
        release_key: str,
        fix_version: str,
        projects_jql: str = DEFAULT_LINK_PROJECTS,
    ) -> Result[str]:
        release_key = (release_key or "").strip().upper()
        fix_version = (fix_version or "").strip()
        if not release_key or not fix_version:
            return Result.failure(ValueError("Укажите release_key и fix_version"))

        try:
            jql = (
                f'project IN ({projects_jql}) AND issuetype IN (Bug, Story) AND fixVersion = "{fix_version}"'
            )
            issues = self.jira.search_issues(jql, limit=500)
            if not issues:
                return Result.success(message="Нет задач для привязки с указанным fixVersion.")

            link_types = self.jira.get_link_types()
            link_type_name = next(
                (name for name in link_types if "part" in name.lower() or "состав" in name.lower()),
                None,
            )
            if not link_type_name:
                return Result.failure(
                    RuntimeError("Не найден подходящий тип связи (part of / состоит из).")
                )

            already_linked = set(self.jira.get_linked_issues(release_key))
            to_link = [
                i
                for i in issues
                if (i.get("key") or "") not in already_linked and (i.get("key") or "") != release_key
            ]
            if not to_link:
                return Result.success(message=f"Все задачи уже привязаны к {release_key}.")

            success = 0
            errors: list[str] = []
            for issue in to_link:
                key = (issue.get("key") or "").strip().upper()
                if not key:
                    continue
                if self.jira.create_issue_link(key, release_key, link_type_name):
                    success += 1
                else:
                    errors.append(key)

            msg = f"Привязано: {success}/{len(to_link)} к {release_key}."
            if errors:
                msg += f" Ошибки: {', '.join(errors[:10])}" + ("..." if len(errors) > 10 else "")
            return Result.success(message=msg)
        except Exception as e:
            return Result.failure(e)
