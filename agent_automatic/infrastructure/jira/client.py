from __future__ import annotations

import logging

from atlassian import Jira  # type: ignore


class JiraClient:
    def __init__(self, *, base_url: str, token: str, verify_ssl: bool = False, logger: logging.Logger | None = None):
        self.base_url = base_url
        self.token = token
        self.verify_ssl = verify_ssl
        self.logger = logger or logging.getLogger(__name__)

        self._jira = Jira(
            url=base_url,
            token=token,
            verify_ssl=verify_ssl,
        )

    def get(self, path: str, **kwargs):
        return self._jira.get(path, **kwargs)

    def post(self, path: str, data=None, advanced_mode: bool = False, **kwargs):
        return self._jira.post(path, data=data, advanced_mode=advanced_mode, **kwargs)

