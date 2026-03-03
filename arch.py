import requests
import urllib3
import logging
import os
from typing import Dict
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Отключаем SSL-предупреждения
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('architecture_field_fix.log')
    ]
)

# Конфигурация
JIRA_URL = os.getenv('JIRA_URL', 'https://jira.sberbank.ru')
JIRA_TOKEN = os.getenv('JIRA_TOKEN')

# Список доступных проектов
AVAILABLE_PROJECTS = {
    '1': {'key': 'HRC', 'name': 'HRC - Human Resources Center'},
    '2': {'key': 'HRM', 'name': 'HRM - Human Resource Management'},
    '3': {'key': 'NEUROUI', 'name': 'NEUROUI - Neural UI'},
    '4': {'key': 'SFILE', 'name': 'SFILE - Smart File'},
    '5': {'key': 'SEARCHCS', 'name': 'SEARCHCS - Search Core'},
}


class ArchitectureFieldFixer:
    def __init__(self, jira_url: str, jira_token: str):
        self.jira_url = jira_url
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {jira_token}',
            'Content-Type': 'application/json'
        })
        self.architecture_field_id = None

    def get_architecture_field_id(self) -> str:
        """
        Автоматически определяет ID поля 'Архитектура' через Jira API
        """
        try:
            response = self.session.get(
                f'{self.jira_url}/rest/api/2/field',
                verify=False
            )
            response.raise_for_status()
            fields = response.json()

            # Ищем поле по названию (регистронезависимо)
            for field in fields:
                if field.get('custom') and 'архитектур' in field.get('name', '').lower():
                    field_id = field['id']
                    logging.info(f"✅ Найдено поле 'Архитектура': {field['name']} (ID: {field_id})")
                    return field_id

            raise ValueError("❌ Не найдено поле 'Архитектура' в Jira")

        except Exception as e:
            logging.error(f"❌ Ошибка при получении полей Jira: {e}")
            raise

    def find_and_fix_stories(self, project_key: str, fix_version: str) -> Dict[str, int]:
        """
        Находит Story по project + fixVersion и устанавливает
        поле Архитектура = "Не влияет на архитектуру"
        """
        try:
            # Получаем ID поля Архитектура
            if not self.architecture_field_id:
                self.architecture_field_id = self.get_architecture_field_id()

            # Получаем ВСЕ Story по project + fixVersion
            jql = f'project = {project_key} AND fixVersion = "{fix_version}" AND issuetype = Story'

            params = {
                'jql': jql,
                'fields': 'key,summary',
                'maxResults': 500
            }

            logging.info(f"🔍 Запрос к Jira: {jql}")
            response = self.session.get(
                f'{self.jira_url}/rest/api/2/search',
                params=params,
                verify=False
            )
            response.raise_for_status()
            data = response.json()

            stories = data.get('issues', [])
            stats = {'total': len(stories), 'need_fix': 0, 'fixed': 0, 'errors': 0}

            logging.info(f"✅ Найдено Story в {fix_version}: {len(stories)}")
            print(f"\n{'='*70}")
            print(f"📊 УСТАНОВКА ПОЛЯ 'АРХИТЕКТУРНЫЕ ИЗМЕНЕНИЯ'")
            print(f"{'='*70}")
            print(f"Проект: {project_key}")
            print(f"Версия: {fix_version}")
            print(f"Всего Story: {len(stories)}\n")

            if len(stories) == 0:
                print("⚠️ Не найдено Story для обработки")
                return stats

            # Подготовка списка задач
            print(f"{'='*70}")
            print(f"🔧 СПИСОК ЗАДАЧ ДЛЯ ОБРАБОТКИ")
            print(f"{'='*70}\n")

            stories_list = []
            for issue in stories:
                issue_key = issue['key']
                summary = issue['fields'].get('summary', 'Без названия')
                stories_list.append((issue_key, summary))
                stats['need_fix'] += 1
                print(f"   • {issue_key}: {summary[:60]}...")

            print(f"\n{'='*70}")
            print(f"⚠️  Будет установлено значение 'Не влияет на архитектуру' для {stats['need_fix']} задач(и)!")

            while True:
                confirm = input("Продолжить? (y/n): ").strip().lower()
                if confirm in ['y', 'yes', 'д', 'да']:
                    print("✅ Подтверждено, начинаем обработку...\n")
                    break
                if confirm in ['n', 'no', 'н', 'нет']:
                    print("❌ Операция отменена пользователем")
                    stats['need_fix'] = 0
                    return stats
                print("⚠️ Некорректный ввод. Введите 'y' для продолжения или 'n' для отмены.")

            print(f"{'='*70}")
            print("🔧 УСТАНОВКА ЗНАЧЕНИЯ")
            print(f"{'='*70}\n")

            # Обрабатываем все Story
            for issue_key, summary in stories_list:
                print(f"🔧 {issue_key}: установка → 'Не влияет на архитектуру'")
                print(f"   └─ {summary[:60]}...")

                success = False

                # Вариант 1: массив с ID и value
                update_data1 = {
                    "fields": {
                        self.architecture_field_id: [
                            {
                                "id": "271300",
                                "value": "Не влияет на архитектуру"
                            }
                        ]
                    }
                }

                response1 = self.session.put(
                    f'{self.jira_url}/rest/api/2/issue/{issue_key}',
                    json=update_data1,
                    verify=False
                )

                if response1.status_code == 204:
                    success = True
                    stats['fixed'] += 1
                    logging.info(f"✅ {issue_key} успешно обновлен")
                    print(f"   └─ ✅ Успешно установлено!\n")
                else:
                    # Вариант 2: массив только с value
                    update_data2 = {
                        "fields": {
                            self.architecture_field_id: [
                                {"value": "Не влияет на архитектуру"}
                            ]
                        }
                    }

                    response2 = self.session.put(
                        f'{self.jira_url}/rest/api/2/issue/{issue_key}',
                        json=update_data2,
                        verify=False
                    )

                    if response2.status_code == 204:
                        success = True
                        stats['fixed'] += 1
                        logging.info(f"✅ {issue_key} успешно обновлен")
                        print(f"   └─ ✅ Успешно установлено (вариант 2)!\n")
                    else:
                        # Вариант 3: массив только с ID
                        update_data3 = {
                            "fields": {
                                self.architecture_field_id: [
                                    {"id": "271300"}
                                ]
                            }
                        }

                        response3 = self.session.put(
                            f'{self.jira_url}/rest/api/2/issue/{issue_key}',
                            json=update_data3,
                            verify=False
                        )

                        if response3.status_code == 204:
                            success = True
                            stats['fixed'] += 1
                            logging.info(f"✅ {issue_key} успешно обновлен")
                            print(f"   └─ ✅ Успешно установлено (вариант 3)!\n")

                if not success:
                    stats['errors'] += 1
                    logging.error(f"❌ {issue_key}: все варианты не сработали")
                    print(f"   └─ ❌ Ошибка обновления")
                    print(f"   └─ Вариант 1: {response1.status_code} - {response1.text[:200]}")
                    print(f"   └─ Вариант 2: {response2.status_code} - {response2.text[:200]}")
                    print(f"   └─ Вариант 3: {response3.status_code} - {response3.text[:200]}\n")

            return stats

        except Exception as e:
            logging.error(f"❌ Критическая ошибка: {e}")
            raise


def select_project() -> str:
    """Интерактивный выбор проекта"""
    print("\n" + "="*70)
    print("📋 ВЫБОР ПРОЕКТА")
    print("="*70)

    for key, project in AVAILABLE_PROJECTS.items():
        print(f"  {key}. {project['name']}")

    print()
    while True:
        choice = input("Введите номер проекта (1-5): ").strip()
        if choice in AVAILABLE_PROJECTS:
            return AVAILABLE_PROJECTS[choice]['key']
        else:
            print("❌ Некорректный выбор. Попробуйте снова.")


def main():
    try:
        # Проверка токена
        if not JIRA_TOKEN:
            raise ValueError("❌ Не найден JIRA_TOKEN в переменных окружения")

        print("\n" + "="*70)
        print("🔧 СКРИПТ УСТАНОВКИ ПОЛЯ 'АРХИТЕКТУРА' В JIRA")
        print("   (Устанавливает значение 'Не влияет на архитектуру')")
        print("="*70)

        # Выбор проекта
        project_key = select_project()

        # Обязательный ввод fixVersion
        fix_version = input("\nВведите fixVersion (например, Minor-2025-10-30): ").strip()
        if not fix_version:
            raise ValueError("❌ fixVersion обязателен для запуска скрипта")

        # Запуск проверки
        fixer = ArchitectureFieldFixer(JIRA_URL, JIRA_TOKEN)

        print(f"\n⏳ Запуск обработки для {project_key} / {fix_version}...")
        stats = fixer.find_and_fix_stories(project_key, fix_version)

        # Итоговая статистика
        print(f"\n{'='*70}")
        print("📊 ИТОГОВАЯ СТАТИСТИКА")
        print(f"{'='*70}")
        print(f"📋 Всего Story в релизе:          {stats['total']}")
        print(f"🔧 Обработано задач:              {stats['need_fix']}")
        print(f"✅ Успешно установлено значение:  {stats['fixed']}")
        print(f"❌ Ошибок при обновлении:         {stats['errors']}")
        print(f"{'='*70}\n")

        if stats['fixed'] > 0:
            print(f"🎉 Успешно обработано {stats['fixed']} из {stats['need_fix']} задач(и)!")
        elif stats['errors'] > 0:
            print(f"⚠️ Не удалось обработать {stats['errors']} задач(и)")
        elif stats['total'] == 0:
            print(f"⚠️ Не найдено Story в релизе {fix_version}")

    except Exception as e:
        logging.error(f"❌ Критическая ошибка: {e}")
        print(f"\n❌ ОШИБКА: {e}")
        exit(1)


if __name__ == "__main__":
    main()
