import os
import json
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

# Загрузка переменных окружения из .env
load_dotenv()


class JiraConfig:
    """Конфигурация для подключения к Jira"""

    def __init__(self, url=None, token=None, verify_ssl=False):
        # Приоритет: параметры -> .env -> по умолчанию
        self.url = url or os.getenv('JIRA_URL', 'https://jira.sberbank.ru')
        self.token = token or os.getenv('JIRA_TOKEN', '')
        self.verify_ssl = verify_ssl

    @classmethod
    def load_from_file(cls, filepath: str) -> 'JiraConfig':
        """Загрузка конфигурации из файла (deprecated, используется .env)"""
        return cls()

    def save_to_file(self, filepath: str):
        """Сохранение конфигурации в файл"""
        directory = os.path.dirname(filepath)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({
                'url': self.url,
                'token': '***HIDDEN***',
                'verify_ssl': self.verify_ssl
            }, f, indent=2)


# Confluence настройки
CONFLUENCE_URL = os.getenv('CONFLUENCE_URL', 'https://confluence.sberbank.ru')
CONFLUENCE_TOKEN = os.getenv('CONFLUENCE_TOKEN')
CONFLUENCE_SPACE_KEY = os.getenv('CONFLUENCE_SPACE_KEY', 'HRTECH')
CONFLUENCE_PARENT_PAGE_TITLE = os.getenv('CONFLUENCE_PARENT_PAGE_TITLE', 'deploy plan 2k')
CONFLUENCE_TEMPLATE_PAGE_ID = os.getenv('CONFLUENCE_TEMPLATE_PAGE_ID', '18532011154')
TEAM_NAME = os.getenv('TEAM_NAME', 'Команда')


# Валидация обязательных параметров
def validate_config():
    """Проверка наличия всех обязательных параметров"""
    required_vars = {
        'JIRA_TOKEN': os.getenv('JIRA_TOKEN'),
        'CONFLUENCE_TOKEN': CONFLUENCE_TOKEN,
        'CONFLUENCE_TEMPLATE_PAGE_ID': CONFLUENCE_TEMPLATE_PAGE_ID
    }

    missing_vars = [var for var, value in required_vars.items() if not value]

    if missing_vars:
        error_msg = f"❌ Отсутствуют обязательные переменные в .env: {', '.join(missing_vars)}"
        logger.error(error_msg)
        return False

    logger.info("✅ Конфигурация загружена из .env успешно")
    logger.info(f"   • Confluence Space: {CONFLUENCE_SPACE_KEY}")
    logger.info(f"   • Parent Page: {CONFLUENCE_PARENT_PAGE_TITLE}")
    logger.info(f"   • Template ID: {CONFLUENCE_TEMPLATE_PAGE_ID}")
    logger.info(f"   • Team: {TEAM_NAME}")
    return True
