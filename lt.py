import os
import requests
from datetime import datetime
from dotenv import load_dotenv
import logging
import re
from urllib3.exceptions import InsecureRequestWarning

# Загрузка переменных окружения
load_dotenv()

# Отключаем предупреждения SSL
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class JiraTaskAnalyzer:
    """Анализатор релизных задач Jira для проверки метрик LT"""

    def __init__(self):
        self.config = {
            'jira': {
                'url': os.getenv('JIRA_URL', 'https://jira.sberbank.ru'),
                'token': os.getenv('JIRA_TOKEN'),
                'verify_ssl': False
            }
        }
        self.jira_headers = {
            'Authorization': f'Bearer {self.config["jira"]["token"]}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        # ПРАВИЛЬНОЕ ПОЛЕ LT
        self.lt_field = 'customfield_25101'

    def make_request(self, url, params=None):
        """Выполнение HTTP запроса"""
        try:
            response = requests.get(
                url,
                headers=self.jira_headers,
                verify=self.config['jira']['verify_ssl'],
                params=params,
                timeout=30
            )
            return response
        except Exception as e:
            logger.error(f"Ошибка запроса к {url}: {e}")
            return None

    def parse_lt_value(self, value):
        """Парсит LT значение из различных форматов"""
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, str):
            # Заменяем запятую на точку
            value = value.replace(',', '.')
            numbers = re.findall(r'\d+\.?\d*', value)
            if numbers:
                try:
                    return float(numbers[0])
                except:
                    pass

        return None

    def get_issue_info(self, issue_key: str):
        """Получает основную информацию о задаче"""
        url = f"{self.config['jira']['url']}/rest/api/2/issue/{issue_key}"
        params = {'fields': f'summary,issuetype,status,{self.lt_field}'}

        response = self.make_request(url, params)

        if not response or response.status_code != 200:
            return None

        data = response.json()
        fields = data.get('fields', {})

        return {
            'key': issue_key,
            'summary': fields.get('summary', ''),
            'issue_type': fields.get('issuetype', {}).get('name', ''),
            'status': fields.get('status', {}).get('name', ''),
            'lt_value': fields.get(self.lt_field)
        }

    def get_linked_issues(self, issue_key: str):
        """Получает все связанные задачи"""
        url = f"{self.config['jira']['url']}/rest/api/2/issue/{issue_key}"
        params = {'fields': 'issuelinks'}

        response = self.make_request(url, params)

        if not response or response.status_code != 200:
            return []

        data = response.json()
        issue_links = data.get('fields', {}).get('issuelinks', [])

        linked_keys = []
        for link in issue_links:
            if 'inwardIssue' in link:
                linked_keys.append(link['inwardIssue']['key'])
            if 'outwardIssue' in link:
                linked_keys.append(link['outwardIssue']['key'])

        return linked_keys

    def analyze_release_task(self, release_key: str, target_lt: float):
        """Полный анализ релизной задачи с проверкой целевого LT"""
        logger.info(f"Начало анализа задачи: {release_key}")

        # Получаем данные релизной задачи
        release_info = self.get_issue_info(release_key)
        if not release_info:
            return {'error': f'Не удалось получить данные для {release_key}'}

        # Парсим LT релиза
        release_lt_raw = release_info['lt_value']
        release_lt = self.parse_lt_value(release_lt_raw) if release_lt_raw else None

        # Получаем связанные задачи
        linked_keys = self.get_linked_issues(release_key)
        logger.info(f"Найдено связанных задач: {len(linked_keys)}")

        if not linked_keys:
            return {
                'release_key': release_key,
                'release_lt': release_lt,
                'linked_issues_count': 0,
                'warning': 'Нет связанных задач для анализа',
                'target_lt': target_lt
            }

        # Анализируем LT связанных задач
        issues_with_lt = []
        issues_without_lt = []
        max_lt = None
        max_lt_issue = None

        for key in linked_keys:
            info = self.get_issue_info(key)
            if not info:
                continue

            lt_raw = info['lt_value']
            lt_parsed = self.parse_lt_value(lt_raw) if lt_raw else None

            if lt_parsed is not None:
                issues_with_lt.append({
                    'key': key,
                    'lt': lt_parsed,
                    'summary': info['summary'],
                    'type': info['issue_type']
                })

                if max_lt is None or lt_parsed > max_lt:
                    max_lt = lt_parsed
                    max_lt_issue = key
            else:
                issues_without_lt.append(key)

        # Формируем результат
        result = {
            'release_key': release_key,
            'release_lt': release_lt,
            'linked_issues_count': len(linked_keys),
            'max_lt': max_lt,
            'max_lt_issue': max_lt_issue,
            'issues_with_lt': issues_with_lt,
            'issues_without_lt': issues_without_lt,
            'target_lt': target_lt,
            'timestamp': datetime.now().isoformat()
        }

        # Проверка целевого LT
        if max_lt:
            if max_lt > target_lt:
                deviation = max_lt - target_lt
                result['target_violation'] = True
                result['target_message'] = f"❌ ПРЕВЫШЕНИЕ НОРМЫ на {deviation:.1f} дней ({((deviation/target_lt)*100):.1f}%)"
            else:
                reserve = target_lt - max_lt
                result['target_violation'] = False
                result['target_message'] = f"✅ НОРМА СОБЛЮДЕНА (запас {reserve:.1f} дней)"

        # Проверка LT релиза
        if release_lt and target_lt:
            if release_lt > target_lt:
                deviation = release_lt - target_lt
                result['release_violation'] = True
                result['release_message'] = f"⚠️ LT релиза превышает норму на {deviation:.1f} дней"
            else:
                reserve = target_lt - release_lt
                result['release_violation'] = False
                result['release_message'] = f"✅ LT релиза в норме (запас {reserve:.1f} дней)"

        # Проверка метрики (LT релиза >= макс LT задач)
        if release_lt and max_lt:
            if release_lt < max_lt:
                result['metric_violation'] = True
                result['metric_message'] = f"⚠️ LT релиза ({release_lt:.1f}) < макс. LT задач ({max_lt:.1f})"
            else:
                result['metric_violation'] = False
                result['metric_message'] = f"✅ LT релиза >= макс. LT задач"

        return result


def run_lt_check_with_target(release_key: str, target_lt: float) -> str:
    """Запуск проверки LT с целевым значением"""
    analyzer = JiraTaskAnalyzer()
    analysis = analyzer.analyze_release_task(release_key, target_lt)
    return format_analysis_report(analysis)


def format_analysis_report(analysis: dict) -> str:
    """Форматирование отчета с читаемым выводом"""
    if 'error' in analysis:
        return f"❌ ОШИБКА: {analysis['error']}"

    if 'warning' in analysis and analysis['linked_issues_count'] == 0:
        return f"⚠️ ПРЕДУПРЕЖДЕНИЕ: {analysis['warning']}"

    lines = []
    target_lt = analysis.get('target_lt', 45.0)

    # ЗАГОЛОВОК
    lines.append("=" * 80)
    lines.append(f"📊 ОТЧЕТ ПО LT МЕТРИКЕ: {analysis['release_key']}")
    lines.append("=" * 80)
    lines.append(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Норма LT: {target_lt} дней")
    lines.append("")

    # ОСНОВНЫЕ ПОКАЗАТЕЛИ
    lines.append("📈 ОСНОВНЫЕ ПОКАЗАТЕЛИ:")
    lines.append(f"  • Связанных задач: {analysis['linked_issues_count']}")

    release_lt = analysis.get('release_lt')
    if release_lt:
        lines.append(f"  • LT релиза: {release_lt:.1f} дней")
    else:
        lines.append(f"  • LT релиза: ❌ НЕ УКАЗАН")

    max_lt = analysis.get('max_lt')
    if max_lt:
        lines.append(f"  • Максимальный LT задач: {max_lt:.1f} дней")
        if analysis.get('max_lt_issue'):
            lines.append(f"  • Задача с макс. LT: {analysis['max_lt_issue']}")
    else:
        lines.append(f"  • Максимальный LT задач: ❌ НЕ НАЙДЕН")

    lines.append("")

    # ПРОВЕРКА НОРМЫ
    lines.append("🎯 " + "=" * 75)
    lines.append("   ПРОВЕРКА СОБЛЮДЕНИЯ НОРМЫ LT")
    lines.append("=" * 80)
    lines.append("")

    if max_lt:
        lines.append(f"Максимальный LT задач: {max_lt:.1f} дней")
        lines.append(f"Целевой LT (норма): {target_lt} дней")
        lines.append("")
        lines.append(f"➤ {analysis.get('target_message', '')}")
        lines.append("")

    if release_lt and analysis.get('release_message'):
        lines.append(f"LT релиза: {release_lt:.1f} дней")
        lines.append(f"➤ {analysis.get('release_message', '')}")
        lines.append("")

    if analysis.get('metric_message'):
        lines.append(f"➤ {analysis.get('metric_message', '')}")
        lines.append("")

    lines.append("=" * 80)
    lines.append("")

    # ЗАДАЧИ С LT
    issues_with_lt = analysis.get('issues_with_lt', [])
    if issues_with_lt:
        lines.append(f"📋 ЗАДАЧИ С ЗАПОЛНЕННЫМ LT ({len(issues_with_lt)}):")
        lines.append("")

        # Сортируем по убыванию LT
        sorted_issues = sorted(issues_with_lt, key=lambda x: x['lt'], reverse=True)

        for item in sorted_issues:
            lt_val = item['lt']
            key = item['key']

            if lt_val > target_lt:
                deviation = lt_val - target_lt
                status = f"❌ ПРЕВЫШЕНИЕ на {deviation:.1f} дней ({((deviation/target_lt)*100):.1f}%)"
                lines.append(f"  🔺 {key}: {lt_val:.1f} дней - {status}")
            else:
                reserve = target_lt - lt_val
                lines.append(f"  ✅ {key}: {lt_val:.1f} дней (запас {reserve:.1f} дней)")

            lines.append(f"     📝 {item.get('summary', '')[:60]}...")

        lines.append("")

    # ЗАДАЧИ БЕЗ LT
    issues_without_lt = analysis.get('issues_without_lt', [])
    if issues_without_lt:
        lines.append(f"❌ ЗАДАЧИ БЕЗ ЗАПОЛНЕННОГО LT ({len(issues_without_lt)}):")
        for key in issues_without_lt:
            lines.append(f"  ⚠️ {key} - LT не указан")
        lines.append("")

    lines.append("=" * 80)
    lines.append("")

    # РЕКОМЕНДАЦИЯ
    lines.append("💡 РЕКОМЕНДАЦИЯ:")
    if not issues_with_lt:
        lines.append(f"   ❌ Необходимо заполнить поле LT (customfield_25101) во всех задачах!")
    elif analysis.get('target_violation'):
        lines.append(f"   ⚠️ Есть задачи с LT > {target_lt} дней. Требуется оптимизация.")
        if analysis.get('metric_violation'):
            lines.append(f"   ⚠️ LT релиза меньше максимального LT задач - обновите релиз!")
    else:
        lines.append(f"   ✅ Все задачи в норме! Продолжайте в том же духе.")

    lines.append("")
    lines.append("=" * 80)

    return '\n'.join(lines)


def run_lt_check(release_key: str) -> str:
    """Обратная совместимость"""
    return run_lt_check_with_target(release_key, 45.0)


if __name__ == '__main__':
    release_key = input("Введите ключ релизной задачи: ").strip()
    if release_key:
        target = input("Целевой LT в днях (Enter = 45): ").strip()
        target_lt = float(target) if target else 45.0
        report = run_lt_check_with_target(release_key, target_lt)
        print(report)
    else:
        print("❌ Ключ задачи не указан")
