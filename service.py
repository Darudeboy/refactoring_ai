import logging
from typing import Dict, List, Optional, Tuple
from atlassian import Jira
import requests
import warnings

from config import JiraConfig

warnings.filterwarnings('ignore')


class JiraService:
    """Сервис для работы с Jira API"""

    def __init__(self, config: JiraConfig):
        self.config = config
        self._jira = None
        self._link_types_cache = None
        self._field_name_map_cache: Optional[Dict[str, str]] = None
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

            # Fallback для tenant'ов, где основной состав релиза хранится через
            # linkedIssues("REL", "consists of") и не виден напрямую в issuelinks.
            if not linked_keys:
                jql_candidates = [
                    f'issue in linkedIssues("{release_key}", "consists of")',
                    f'issue in linkedIssues("{release_key}")',
                ]
                for jql in jql_candidates:
                    try:
                        data = self.jira.jql(jql, limit=5000)
                        issues = data.get("issues", []) if isinstance(data, dict) else []
                        for issue in issues:
                            key = str((issue or {}).get("key", "")).strip().upper()
                            if key:
                                linked_keys.append(key)
                        if linked_keys:
                            break
                    except Exception as e:
                        self.logger.error("JQL fallback linkedIssues failed for %s: %s", release_key, e)

            unique = sorted(set(linked_keys))
            self.logger.info("Linked issues for %s: %s", release_key, len(unique))
            return unique
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

    def get_issue_comments(self, issue_key: str) -> List[dict]:
        """Получение комментариев Jira-задачи."""
        try:
            response = self.jira.get(f"/rest/api/2/issue/{issue_key}/comment")
            comments = response.get("comments", []) if isinstance(response, dict) else []
            if isinstance(comments, list):
                return comments
            return []
        except Exception as e:
            self.logger.error(f"Ошибка получения комментариев для {issue_key}: {e}")
            return []

    def get_field_name_map(self) -> Dict[str, str]:
        """Карта field_id -> display name из Jira."""
        if self._field_name_map_cache is not None:
            return self._field_name_map_cache
        try:
            fields = self.jira.get("/rest/api/2/field")
            if not isinstance(fields, list):
                self._field_name_map_cache = {}
                return self._field_name_map_cache
            result: Dict[str, str] = {}
            for item in fields:
                if not isinstance(item, dict):
                    continue
                field_id = str(item.get("id", "")).strip()
                field_name = str(item.get("name", "")).strip()
                if field_id:
                    result[field_id] = field_name
            self._field_name_map_cache = result
            return result
        except Exception as e:
            self.logger.error(f"Ошибка получения списка полей Jira: {e}")
            self._field_name_map_cache = {}
            return self._field_name_map_cache

    def get_dev_status_prs(self, issue_id: str) -> List[dict]:
        """Получение PR из панели Development (Stash/Bitbucket интеграция)."""
        try:
            url = (
                f"/rest/dev-status/latest/issue/detail"
                f"?issueId={issue_id}"
                f"&applicationType=stash&dataType=pullrequest"
            )
            response = self.jira.get(url)
            prs: List[dict] = []
            for detail in (response or {}).get("detail", []):
                for pr in detail.get("pullRequests", []):
                    prs.append(pr)
            return prs
        except Exception as e:
            self.logger.error(
                f"Ошибка получения dev-status PR для issue {issue_id}: {e}"
            )
            return []

    def get_dev_status_summary(self, issue_key: str) -> Dict[str, object]:
        """
        Получение сводки Development panel:
        /rest/dev-status/1.0/issue/summary?issueId=...
        """
        safe_key = (issue_key or "").strip().upper()
        issue_id = self.get_issue_id(safe_key)
        if not issue_id:
            return {}
        try:
            return self.jira.get(f"/rest/dev-status/1.0/issue/summary?issueId={issue_id}") or {}
        except Exception as e:
            self.logger.error("Ошибка получения dev-status summary для %s: %s", safe_key, e)
            return {}

    def get_available_transitions(self, issue_key: str) -> List[dict]:
        """Получение доступных переходов статуса для задачи"""
        try:
            response = self.jira.get(f'/rest/api/2/issue/{issue_key}/transitions')
            return response.get('transitions', [])
        except Exception as e:
            self.logger.error(f"Ошибка получения переходов для {issue_key}: {e}")
            return []

    def get_issue_id(self, issue_key: str) -> Optional[str]:
        """Возвращает numeric issueId Jira для ключа задачи."""
        safe_key = (issue_key or "").strip().upper()
        if not safe_key:
            return None
        issue = self.get_issue_details(safe_key)
        if not issue:
            return None
        issue_id = str(issue.get("id", "")).strip()
        return issue_id or None

    def get_sber_test_report(self, issue_key: str) -> str:
        """
        Получает HTML блока 'Отчет о тестировании' из plugin endpoint.
        Используется как fallback, когда Jira fields/renderedFields не содержат нужные маркеры.
        """
        safe_key = (issue_key or "").strip().upper()
        endpoint = (
            f"{self.config.url.rstrip('/')}"
            f"/rest/sber-test-report/1.0/sber-test-report/rqgiftstatushtml"
        )
        params = {"issueKey": safe_key}
        headers = {
            "Accept": "text/html, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Authorization": f"Bearer {self.config.token}",
        }
        try:
            response = requests.get(
                endpoint,
                params=params,
                headers=headers,
                timeout=20,
                verify=self.config.verify_ssl,
            )
            if response.status_code == 200:
                return response.text or ""

            # Fallback: часть tenant'ов принимает только issueId вместо issueKey.
            issue_id = self.get_issue_id(safe_key)
            if issue_id:
                response_by_id = requests.get(
                    endpoint,
                    params={"issueId": issue_id},
                    headers=headers,
                    timeout=20,
                    verify=self.config.verify_ssl,
                )
                if response_by_id.status_code == 200:
                    return response_by_id.text or ""

            self.logger.error(
                "sber-test-report HTTP %s for %s: %s",
                response.status_code,
                safe_key,
                (response.text or "")[:200],
            )
        except Exception as e:
            self.logger.error("Failed to fetch sber-test-report for %s: %s", safe_key, e)
        return ""

    def get_qgm_status(self, issue_key: str) -> Tuple[bool, str, Optional[dict]]:
        """
        Получение RQG-данных по endpoint:
        /rest/release/1/qgm?issueId=<numeric_issue_id>
        """
        safe_issue = (issue_key or "").strip().upper()
        issue_id = self.get_issue_id(safe_issue)
        if not issue_id:
            return False, f"Не удалось определить issueId для {safe_issue}", None

        endpoint = f"{self.config.url.rstrip('/')}/rest/release/1/qgm"
        params = {"issueId": issue_id}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {self.config.token}",
            "X-Requested-With": "XMLHttpRequest",
            "X-Atlassian-Token": "no-check",
        }

        def _short_body(text: str, limit: int = 300) -> str:
            body = (text or "").strip().replace("\n", " ")
            if len(body) <= limit:
                return body
            return f"{body[: limit - 3]}..."

        try:
            # 1) Приоритетно: POST с query params (как в ручном успешном сценарии Jira UI)
            response = requests.post(
                url=endpoint,
                params=params,
                headers=headers,
                timeout=30,
                verify=self.config.verify_ssl,
            )
            if 200 <= response.status_code < 300:
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        return True, "QGM OK (POST)", payload
                except Exception:
                    text = (response.text or "").strip()
                    if text:
                        return True, "QGM OK (POST non-json)", {"raw_text": text}
                    return False, "QGM failed: POST returned empty non-json body", None

            # 2) POST+JSON fallback.
            response_json = requests.post(
                url=endpoint,
                params=params,
                json={"issueId": int(issue_id)},
                headers={**headers, "Content-Type": "application/json"},
                timeout=30,
                verify=self.config.verify_ssl,
            )
            if 200 <= response_json.status_code < 300:
                try:
                    payload = response_json.json()
                    if isinstance(payload, dict):
                        return True, "QGM OK (POST+JSON)", payload
                except Exception:
                    text = (response_json.text or "").strip()
                    if text:
                        return True, "QGM OK (POST+JSON non-json)", {"raw_text": text}

            # 3) GET fallback.
            response_get = requests.get(
                url=endpoint,
                params=params,
                headers=headers,
                timeout=30,
                verify=self.config.verify_ssl,
            )
            if 200 <= response_get.status_code < 300:
                try:
                    payload = response_get.json()
                    if isinstance(payload, dict):
                        return True, "QGM OK (GET)", payload
                except Exception:
                    text = (response_get.text or "").strip()
                    if text:
                        return True, "QGM OK (GET non-json)", {"raw_text": text}

            return (
                False,
                f"QGM failed: POST HTTP {response.status_code}, POST+JSON HTTP {response_json.status_code}, "
                f"GET HTTP {response_get.status_code}",
                None,
            )
        except Exception as e:
            self.logger.error("Ошибка QGM endpoint для issue=%s: %s", safe_issue, e)
            return False, f"QGM failed: POST error: {e}", None

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

    def transition_issue_by_id(self, issue_key: str, transition_id: str) -> Tuple[bool, str]:
        """Перевод задачи по transition ID."""
        safe_key = (issue_key or "").strip().upper()
        safe_transition_id = str(transition_id or "").strip()
        if not safe_key or not safe_transition_id:
            return False, "Не указан issue_key или transition_id"
        try:
            response = self.jira.post(
                f"/rest/api/2/issue/{safe_key}/transitions",
                data={"transition": {"id": safe_transition_id}},
                advanced_mode=True,
            )
            if response.status_code in (200, 204):
                return True, f"{safe_key} переведена по transition id {safe_transition_id}"
            return False, f"Jira вернул код {response.status_code} для transition id {safe_transition_id}"
        except Exception as e:
            self.logger.error(f"Ошибка перевода {safe_key} по transition id {safe_transition_id}: {e}")
            return False, f"Ошибка перевода по transition id: {e}"

    @staticmethod
    def normalize_status(status: str) -> str:
        return (status or "").strip().lower()

    def status_in(self, status: str, allowed_statuses: List[str]) -> bool:
        normalized = self.normalize_status(status)
        allowed = {self.normalize_status(item) for item in (allowed_statuses or [])}
        return normalized in allowed

    def collect_release_related_issues(
        self,
        release_key: str,
        max_depth: int = 2,
    ) -> Dict[str, dict]:
        """Собирает релиз и связанные задачи (BFS по ссылкам/сабтаскам)."""
        discovered: Dict[str, dict] = {}
        queue: List[Tuple[str, int]] = [((release_key or "").strip().upper(), 0)]
        while queue:
            issue_key, depth = queue.pop(0)
            if not issue_key or issue_key in discovered or depth > max_depth:
                continue
            issue = self.get_issue_details(issue_key)
            if not issue:
                continue
            discovered[issue_key] = issue

            fields = issue.get("fields", {}) or {}
            for sub in fields.get("subtasks", []) or []:
                sub_key = sub.get("key")
                if sub_key and sub_key not in discovered:
                    queue.append((sub_key, depth + 1))

            for link in fields.get("issuelinks", []) or []:
                outward = (link.get("outwardIssue") or {}).get("key")
                inward = (link.get("inwardIssue") or {}).get("key")
                for linked in (outward, inward):
                    if linked and linked not in discovered:
                        queue.append((linked, depth + 1))
        return discovered
