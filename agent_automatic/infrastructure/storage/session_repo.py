from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

from agent_automatic.domain.commands.models import ConversationState


class SessionRepo:
    def __init__(self, path: str):
        self.path = path
        self._cache: dict[str, ConversationState] = {}
        self._load_all()

    def get(self, conversation_id: str) -> ConversationState:
        key = conversation_id or "default"
        state = self._cache.get(key)
        if state:
            return state
        state = ConversationState()
        self._cache[key] = state
        return state

    def save(self, conversation_id: str, state: ConversationState):
        key = conversation_id or "default"
        self._cache[key] = state
        self._save_all()

    def _load_all(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if not isinstance(data, dict):
                return
            for cid, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                self._cache[str(cid)] = ConversationState(
                    last_release_key=payload.get("last_release_key"),
                    pending_intent=payload.get("pending_intent"),
                    pending_slots=dict(payload.get("pending_slots") or {}),
                    last_project_key=payload.get("last_project_key"),
                )
        except Exception:
            return

    def _save_all(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        data: dict[str, Any] = {cid: asdict(state) for cid, state in self._cache.items()}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

