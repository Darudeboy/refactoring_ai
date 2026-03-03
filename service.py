import logging
from typing import Dict, List, Optional, Tuple
from atlassian import Jira
import warnings

from config import JiraConfig

warnings.filterwarnings('ignore')


class JiraService:
    """Сервис для работы с Jira API"""

    def __init__(self, config: JiraConfig):
        self.config = config
        self._jira = None
        self._link_types_cache = None
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    def jira(self) -> Jira:
        """Ленивая инициализация подключения к Jira"""
        if self._jira is None:
            self._jira = Jira(
                url=self.config.url,
                token=self.config.token,
                verify_ssl=self.config.verify_ssl,
            )
        return self._jira

    def test_connection(self) -> Tuple[bool, str]:
        """Проверка подключения к Jira"""
        try:
            self.jira.myself()
            return True, "Подключение успешно установлено"
        except Exception as e:
            return False, f"Ошибка подключения: {str(e)}"

    def get_link_types(self) -> Dict[str, dict]:
        """Получение типов связей с кэшированием"""
        if self._link_types_cache is None:
            try:
                link_types = self.jira.get_issue_link_types()
                self._link_types_cache = {lt['name']: lt for lt in link_types}
            except Exception as e:
                self.logger.error(f"Ошибка получения типов связей: {e}")
                self._link_types_cache = {}
        return self._link_types_cache

    def get_linked_issues(self, release_key: str) -> List[str]:
        """Получение связанных задач"""
        try:
            url = f'/rest/api/2/issue/{release_key}?expand=renderedFields,issuelinks'
            response = self.jira.get(url)
            linked_keys: List[str] = []
            for link in response.get('fields', {}).get('issuelinks', []):
                if 'outwardIssue' in link:
                    linked_keys.append(link['outwardIssue']['key'])
                elif 'inwardIssue' in link:
                    linked_keys.append(link['inwardIssue']['key'])
            return list(set(linked_keys))
        except Exception as e:
            self.logger.error(f"Не удалось получить связи для {release_key}: {e}")
            return []

    def search_issues(self, jql: str, limit: int = 500) -> List[dict]:
        """Поиск задач по JQL"""
        try:
            data = self.jira.jql(jql, limit=limit)
            return data.get('issues', [])
        except Exception as e:
            self.logger.error(f"Ошибка поиска задач: {e}")
            raise

    def create_issue_link(self, from_issue: str, to_issue: str, link_type: str) -> bool:
        """Создание связи между задачами"""
        try:
            url = '/rest/api/2/issueLink'
            payload = {
                "type": {"name": link_type},
                "inwardIssue": {"key": from_issue},
                "outwardIssue": {"key": to_issue},
            }
            response = self.jira.post(url, data=payload, advanced_mode=True)
            return response.status_code == 201
        except Exception as e:
            self.logger.error(f"Ошибка создания связи {from_issue} -> {to_issue}: {e}")
            return False

    def delete_issue_link(self, link_id: str) -> bool:
        """Удаление связи"""
        try:
            response = self.jira.delete(f'/rest/api/2/issueLink/{link_id}', advanced_mode=True)
            return response.status_code == 204
        except Exception as e:
            self.logger.error(f"Ошибка удаления связи {link_id}: {e}")
            return False

    def get_issue_details(
        self,
        issue_key: str,
        fields: Optional[str] = None,
        expand: str = "issuelinks",
    ) -> Optional[dict]:
        """Получение детальной информации о задаче"""
        try:
            params: List[str] = []
            if expand:
                params.append(f"expand={expand}")
            if fields:
                params.append(f"fields={fields}")

            query = f"?{'&'.join(params)}" if params else ""
            return self.jira.get(f"/rest/api/2/issue/{issue_key}{query}")
        except Exception as e:
            self.logger.error(f"Ошибка получения информации о {issue_key}: {e}")
            return None

    def get_issue_remote_links(self, issue_key: str) -> List[dict]:
        """Получение удаленных ссылок задачи (включая PR-ссылки из dev-интеграций)."""
        try:
            response = self.jira.get(f"/rest/api/2/issue/{issue_key}/remotelink")
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            self.logger.error(f"Ошибка получения remote links для {issue_key}: {e}")
            return []

    def get_available_transitions(self, issue_key: str) -> List[dict]:
        """Получение доступных переходов статуса для задачи"""
        try:
            response = self.jira.get(f'/rest/api/2/issue/{issue_key}/transitions')
            return response.get('transitions', [])
        except Exception as e:
            self.logger.error(f"Ошибка получения переходов для {issue_key}: {e}")
            return []

    def transition_issue(self, issue_key: str, target_status: str) -> Tuple[bool, str]:
        """Перевод задачи в целевой статус по названию статуса"""
        try:
            transitions = self.get_available_transitions(issue_key)
            if not transitions:
                return False, f"Для {issue_key} не найдено доступных переходов"

            target = (target_status or "").strip().lower()
            if not target:
                return False, "Целевой статус не указан"

            matched_transition = None
            for transition in transitions:
                name = transition.get("name", "")
                if name.lower() == target:
                    matched_transition = transition
                    break

            if not matched_transition:
                for transition in transitions:
                    name = transition.get("name", "")
                    if target in name.lower():
                        matched_transition = transition
                        break

            if not matched_transition:
                options = ", ".join(t.get("name", "Unknown") for t in transitions)
                return False, f"Переход '{target_status}' не найден. Доступно: {options}"

            payload = {"transition": {"id": matched_transition["id"]}}
            response = self.jira.post(
                f'/rest/api/2/issue/{issue_key}/transitions',
                data=payload,
                advanced_mode=True,
            )

            success = response.status_code in (200, 204)
            if success:
                return True, f"{issue_key} переведена в статус '{matched_transition.get('name')}'"
            return False, f"Jira вернул код {response.status_code} при переводе {issue_key}"
        except Exception as e:
            self.logger.error(f"Ошибка перевода {issue_key} в '{target_status}': {e}")
            return False, f"Ошибка перевода статуса: {e}"
