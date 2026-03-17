from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class HistoryRepo:
    path: str
    max_size: int = 100
    history: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.history is None:
            self.history = []
        self.load()

    def add(self, operation: str, details: dict[str, Any]):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "details": details,
        }
        self.history.insert(0, entry)
        if len(self.history) > self.max_size:
            self.history.pop()
        self.save()

    def recent(self, count: int = 10) -> list[dict[str, Any]]:
        return self.history[:count]

    def save(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False)

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.history = json.load(f) or []
            if not isinstance(self.history, list):
                self.history = []
        except Exception:
            self.history = []

