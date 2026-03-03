import os
import json
from datetime import datetime
from typing import Dict, List


class OperationHistory:
    """Класс для хранения истории операций"""

    def __init__(self, max_size: int = 100):
        self.history: List[Dict] = []
        self.max_size = max_size

    def add(self, operation: str, details: Dict):
        """Добавление операции в историю"""
        entry = {
            'timestamp': datetime.now().isoformat(),
            'operation': operation,
            'details': details,
        }
        self.history.insert(0, entry)
        if len(self.history) > self.max_size:
            self.history.pop()

    def get_recent(self, count: int = 10) -> List[Dict]:
        """Получение последних операций"""
        return self.history[:count]

    def save_to_file(self, filepath: str):
        """Сохранение истории в файл"""
        directory = os.path.dirname(filepath)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False)

    def load_from_file(self, filepath: str):
        """Загрузка истории из файла"""
        if not os.path.exists(filepath):
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                self.history = json.load(f)
        except Exception as e:
            print(f"Ошибка загрузки истории: {e}")
            self.history = []
