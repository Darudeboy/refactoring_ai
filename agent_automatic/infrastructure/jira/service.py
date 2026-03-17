from __future__ import annotations

import logging
from typing import Optional, Tuple

import requests

from agent_automatic.infrastructure.jira.client import JiraClient


class JiraService:
    def __init__(self, client: JiraClient, *, logger: logging.Logger | None = None):
        self.client = client
        self.logger = logger or logging.getLogger(__name__)

    def get_issue_details(self, issue_key: str, expand: str | None = None) -> Optional[dict]:
        safe_key = (issue_key or "").strip().upper()
        if not safe_key:
            return None
        path = f"/rest/api/2/issue/{safe_key}"
        try:
            params = {"expand": expand} if expand else None
            return self.client.get(path, params=params) or None
        except Exception as e:
            self.logger.error("Ошибка получения issue %s: %s", safe_key, e)
            return None

    def get_linked_issues(self, release_key: str) -> list[str]:
        issue = self.get_issue_details(release_key, expand="issuelinks")
        if not issue:
            return []
        keys: set[str] = set()
        for link in (issue.get("fields", {}) or {}).get("issuelinks", []) or []:
            outward = link.get("outwardIssue")
            inward = link.get("inwardIssue")
            if outward and outward.get("key"):
                keys.add(outward["key"])
            if inward and inward.get("key"):
                keys.add(inward["key"])
        return sorted(keys)

    def get_field_name_map(self) -> dict[str, str]:
        try:
            fields = self.client.get("/rest/api/2/field") or []
            if not isinstance(fields, list):
                return {}
            return {str(f.get("id")): str(f.get("name")) for f in fields if isinstance(f, dict) and f.get("id")}
        except Exception as e:
            self.logger.error("Ошибка получения карты полей Jira: %s", e)
            return {}

    def get_issue_comments(self, issue_key: str) -> list[dict]:
        safe_key = (issue_key or "").strip().upper()
        if not safe_key:
            return []
        try:
            data = self.client.get(f"/rest/api/2/issue/{safe_key}/comment") or {}
            comments = data.get("comments", []) if isinstance(data, dict) else []
            return comments if isinstance(comments, list) else []
        except Exception as e:
            self.logger.error("Ошибка получения комментариев %s: %s", safe_key, e)
            return []

    def get_available_transitions(self, issue_key: str) -> list[dict]:
        safe_key = (issue_key or "").strip().upper()
        if not safe_key:
            return []
        try:
            response = self.client.get(f"/rest/api/2/issue/{safe_key}/transitions") or {}
            transitions = response.get("transitions", []) if isinstance(response, dict) else []
            return transitions if isinstance(transitions, list) else []
        except Exception as e:
            self.logger.error("Ошибка получения переходов для %s: %s", safe_key, e)
            return []

    def get_issue_id(self, issue_key: str) -> Optional[str]:
        issue = self.get_issue_details(issue_key)
        if not issue:
            return None
        issue_id = str(issue.get("id", "")).strip()
        return issue_id or None

    def get_dev_status_summary(self, issue_key: str) -> dict:
        safe_key = (issue_key or "").strip().upper()
        issue_id = self.get_issue_id(safe_key)
        if not issue_id:
            return {}
        try:
            return self.client.get(f"/rest/dev-status/1.0/issue/summary?issueId={issue_id}") or {}
        except Exception as e:
            self.logger.error("Ошибка получения dev-status summary для %s: %s", safe_key, e)
            return {}

    def get_sber_test_report(self, issue_key: str) -> str:
        safe_key = (issue_key or "").strip().upper()
        endpoint = f"{self.client.base_url.rstrip('/')}/rest/sber-test-report/1.0/sber-test-report/rqgiftstatushtml"
        params = {"issueKey": safe_key}
        headers = {
            "Accept": "text/html, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Authorization": f"Bearer {self.client.token}",
        }
        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=20, verify=self.client.verify_ssl)
            if response.status_code == 200:
                return response.text or ""

            issue_id = self.get_issue_id(safe_key)
            if issue_id:
                response_by_id = requests.get(
                    endpoint,
                    params={"issueId": issue_id},
                    headers=headers,
                    timeout=20,
                    verify=self.client.verify_ssl,
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
        safe_issue = (issue_key or "").strip().upper()
        issue_id = self.get_issue_id(safe_issue)
        if not issue_id:
            return False, f"Не удалось определить issueId для {safe_issue}", None

        endpoint = f"{self.client.base_url.rstrip('/')}/rest/release/1/qgm"
        params = {"issueId": issue_id}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {self.client.token}",
            "X-Requested-With": "XMLHttpRequest",
            "X-Atlassian-Token": "no-check",
        }

        try:
            response = requests.post(
                url=endpoint,
                params=params,
                headers=headers,
                timeout=30,
                verify=self.client.verify_ssl,
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

            response_json = requests.post(
                url=endpoint,
                params=params,
                json={"issueId": int(issue_id)},
                headers={**headers, "Content-Type": "application/json"},
                timeout=30,
                verify=self.client.verify_ssl,
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

            response_get = requests.get(
                url=endpoint,
                params=params,
                headers=headers,
                timeout=30,
                verify=self.client.verify_ssl,
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

    def transition_issue_by_id(self, issue_key: str, transition_id: str) -> Tuple[bool, str]:
        safe_key = (issue_key or "").strip().upper()
        safe_transition_id = str(transition_id or "").strip()
        if not safe_key or not safe_transition_id:
            return False, "Не указан issue_key или transition_id"
        try:
            response = self.client.post(
                f"/rest/api/2/issue/{safe_key}/transitions",
                data={"transition": {"id": safe_transition_id}},
                advanced_mode=True,
            )
            if getattr(response, "status_code", None) in (200, 204):
                return True, f"{safe_key} переведена по transition id {safe_transition_id}"
            return False, f"Jira вернул код {getattr(response, 'status_code', 'unknown')} для transition id {safe_transition_id}"
        except Exception as e:
            self.logger.error("Ошибка перевода %s по transition id %s: %s", safe_key, safe_transition_id, e)
            return False, f"Ошибка перевода по transition id: {e}"

    def transition_issue(self, issue_key: str, target_status: str) -> Tuple[bool, str]:
        safe_key = (issue_key or "").strip().upper()
        target = (target_status or "").strip().lower()
        if not safe_key or not target:
            return False, "Целевой статус не указан"
        try:
            transitions = self.get_available_transitions(safe_key)
            if not transitions:
                return False, f"Для {safe_key} не найдено доступных переходов"

            matched = None
            for t in transitions:
                name = str(t.get("name", ""))
                if name.lower() == target:
                    matched = t
                    break
            if not matched:
                for t in transitions:
                    name = str(t.get("name", ""))
                    if target in name.lower():
                        matched = t
                        break

            if not matched:
                options = ", ".join(str(t.get("name", "Unknown")) for t in transitions)
                return False, f"Переход '{target_status}' не найден. Доступно: {options}"

            payload = {"transition": {"id": matched["id"]}}
            response = self.client.post(
                f"/rest/api/2/issue/{safe_key}/transitions",
                data=payload,
                advanced_mode=True,
            )
            success = getattr(response, "status_code", None) in (200, 204)
            if success:
                return True, f"{safe_key} переведена в статус '{matched.get('name')}'"
            return False, f"Jira вернул код {getattr(response, 'status_code', 'unknown')} при переводе {safe_key}"
        except Exception as e:
            self.logger.error("Ошибка перевода %s в '%s': %s", safe_key, target_status, e)
            return False, f"Ошибка перевода статуса: {e}"

