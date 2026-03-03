import requests
from atlassian import Confluence
import urllib3
import webbrowser
import logging
import os
import sys
import html
from typing import List, Tuple
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Отключаем предупреждения SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('jira_sync.log')]
)

# Доступные проекты
AVAILABLE_PROJECTS = {
    '1': {'key': 'HRC', 'name': 'HRC - Human Resources Center'},
    '2': {'key': 'HRM', 'name': 'HRM - Human Resource Management'},
    '3': {'key': 'NEUROUI', 'name': 'NEUROUI - Neural UI'},
    '4': {'key': 'SFILE', 'name': 'SFILE - Smart File'},
    '5': {'key': 'SEARCHCS', 'name': 'SEARCHCS - Search Core'},
    '6': {'key': 'NEURO', 'name': 'NEURO - Neural'},
    '7': {'key': 'HRPDEV', 'name': 'HRPDEV - HR Platform Dev'}
}

config = {
    'jira': {
        'url': os.getenv('JIRA_URL', 'https://jira.sberbank.ru'),
        'token': os.getenv('JIRA_TOKEN'),
    },
    'confluence': {
        'url': os.getenv('CONFLUENCE_URL', 'https://confluence.sberbank.ru'),
        'token': os.getenv('CONFLUENCE_TOKEN'),
        'space': os.getenv('CONFLUENCE_SPACE', 'HRTECH'),
        'parent_page_id': int(os.getenv('CONFLUENCE_PARENT_PAGE_ID', '20073677085')),
        'verify_ssl': False
    }
}


class JiraConfluenceSync:
    def __init__(self, config_data: dict, project_key: str):
        self.config = config_data
        self.target_project = project_key
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {config_data["jira"]["token"]}',
            'Content-Type': 'application/json'
        })
        self.confluence = None

    def initialize_confluence(self) -> None:
        try:
            self.confluence = Confluence(
                url=self.config['confluence']['url'],
                token=self.config['confluence']['token'],
                verify_ssl=self.config['confluence']['verify_ssl']
            )
        except Exception as e:
            logging.error(f"Confluence error: {e}")
            raise

    def get_release_links_direct(self, release_key: str) -> List[Tuple[str, str, str, str]]:
        try:
            logging.info(f"🔍 Читаем задачу релиза: {release_key}")
            response = self.session.get(
                f'{self.config["jira"]["url"]}/rest/api/2/issue/{release_key}',
                params={'fields': 'issuelinks'},
                verify=False
            )
            response.raise_for_status()
            links = response.json().get('fields', {}).get('issuelinks', [])

            target_keys = []
            for link in links:
                target_issue = None
                link_type = link.get('type', {})

                if 'outwardIssue' in link:
                    if 'consists of' in link_type.get('outward', '').lower():
                        target_issue = link['outwardIssue']
                elif 'inwardIssue' in link:
                    if 'consists of' in link_type.get('inward', '').lower():
                        target_issue = link['inwardIssue']

                if target_issue and target_issue['key'].startswith(self.target_project + '-'):
                    target_keys.append(target_issue['key'])

            if not target_keys:
                logging.warning(f"⚠️ В релизе не найдено связей 'consists of' для проекта {self.target_project}")
                return []

            logging.info(f"🔗 Найдено ключей: {len(target_keys)}. Запрашиваем детали и описания...")
            jql = f"key in ({','.join(target_keys)})"

            search_response = self.session.get(
                f'{self.config["jira"]["url"]}/rest/api/2/search',
                params={
                    'jql': jql,
                    'fields': 'summary,issuetype,description',
                    'maxResults': 1000
                },
                verify=False
            )
            search_response.raise_for_status()
            issues_data = search_response.json().get('issues', [])

            found_issues = []
            for issue in issues_data:
                key = issue['key']
                fields = issue.get('fields', {})
                summary = fields.get('summary', 'Без названия')
                description = fields.get('description')
                if description is None:
                    description = ""
                issue_type = fields.get('issuetype', {}).get('name', 'Task')
                found_issues.append((key, summary, issue_type, description))

            found_issues.sort(key=lambda x: x[0])
            return found_issues

        except Exception as e:
            logging.error(f"Ошибка получения данных из Jira: {e}")
            raise

    def count_issues_by_type(self, issues):
        counts = {}
        for _, _, issue_type, _ in issues:
            normalized_type = issue_type.lower()
            if 'story' in normalized_type or 'история' in normalized_type:
                key = 'Story'
            elif 'bug' in normalized_type or 'дефект' in normalized_type:
                key = 'Bug'
            else:
                key = issue_type
            counts[key] = counts.get(key, 0) + 1
        return counts

    def generate_content(self, release_key: str, issues: List[Tuple[str, str, str, str]]) -> str:
        rows = []
        for key, summary, issue_type, _ in issues:
            safe_summary = html.escape(summary)
            macro = f'<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">{key}</ac:parameter><ac:parameter ac:name="showSummary">true</ac:parameter></ac:structured-macro>'
            rows.append(f'<tr><td>{macro}</td><td>{issue_type}</td><td>{safe_summary}</td></tr>')

        table_html = f'<table class="wrapped"><thead><tr><th>Задача</th><th>Тип</th><th>Название</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'

        product_items = []
        for _, summary, _, description in issues:
            safe_summary = html.escape(summary)
            safe_desc = html.escape(description)
            clean_desc = safe_desc.replace('\n', '<br/>')

            if clean_desc:
                item = f"<li><strong>{safe_summary}</strong><br/><span style='color: #5e6c84; font-size: 90%;'>{clean_desc}</span></li>"
            else:
                item = f"<li><strong>{safe_summary}</strong></li>"
            product_items.append(item)

        product_list = "".join(product_items)

        stories_items = []
        for _, summary, issue_type, description in issues:
            if 'story' in issue_type.lower() or 'история' in issue_type.lower():
                safe_summary = html.escape(summary)
                safe_desc = html.escape(description)
                clean_desc = safe_desc.replace('\n', '<br/>')

                if clean_desc:
                    stories_items.append(f"<li><strong>{safe_summary}</strong><br/><span style='color: #5e6c84;'>{clean_desc}</span></li>")
                else:
                    stories_items.append(f"<li><strong>{safe_summary}</strong></li>")

        tobe_list = "".join(stories_items) if stories_items else "<p>Нет Stories</p>"

        counts = self.count_issues_by_type(issues)
        stats_list = "".join([f"<li><strong>{k}:</strong> {v} шт.</li>" for k, v in counts.items()])

        return f"""
        <h1>Бизнес-требование и функциональное решение [{release_key}]</h1>
        <p>Основано на релизе: <ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">{release_key}</ac:parameter></ac:structured-macro></p>
        <p>Требование на доработке. После окончания работы необходимо передать его на валидацию.</p>
        
        <h2>ID фичи в JIRA</h2>
        {table_html}
        
        <h2>Продукт (детализация)</h2>
        <ul>{product_list}</ul>
        
        <h2>Процесс AS IS</h2>
        <p>Текущее состояние процесса...</p>
        
        <h2>Описание процесса TO BE (Stories)</h2>
        <ul>{tobe_list}</ul>
        
        <h2>Критерии приемки</h2>
        <p>Тестированием не выявлены отклонения.</p>
        <ul>{stats_list}</ul>
        """

    def update_page(self, release_key: str):
        self.initialize_confluence()

        issues = self.get_release_links_direct(release_key)

        title = f"[{release_key}] Бизнес-требование и функциональное решение"
        content = self.generate_content(release_key, issues)

        existing = self.confluence.get_page_by_title(self.config['confluence']['space'], title)

        if existing:
            logging.info(f"Обновляем страницу ID: {existing['id']}")
            self.confluence.update_page(existing['id'], title, content, representation="storage", minor_edit=False)
            page_id = existing['id']
        else:
            logging.info("Создаем новую страницу...")
            new_page = self.confluence.create_page(self.config['confluence']['space'], title, content, parent_id=self.config['confluence']['parent_page_id'], representation="storage")
            page_id = new_page['id']

        try:
            self.confluence.set_page_label(page_id, "br2")
            self.confluence.set_page_label(page_id, "fr2")
        except Exception as e:
            logging.warning(f"Не удалось поставить метки: {e}")

        return f"{self.config['confluence']['url']}/pages/viewpage.action?pageId={page_id}"


def select_project():
    print("\n📋 Выберите проект:")
    for key, value in AVAILABLE_PROJECTS.items():
        print(f"{key}. {value['name']}")
    while True:
        choice = input("Номер (1-7): ").strip()
        if choice in AVAILABLE_PROJECTS:
            return AVAILABLE_PROJECTS[choice]['key']


def main():
    try:
        if not config['jira']['token']:
            print("❌ Нет токена JIRA_TOKEN в .env")
            sys.exit(1)

        # 🤖 РЕЖИМ БОТА: если скрипт запущен с аргументами `python bt3.py HRPRELEASE-123 NEURO`
        if len(sys.argv) == 3:
            release_key = sys.argv[1].upper()
            project_key = sys.argv[2].upper()
            sync = JiraConfluenceSync(config, project_key)
            url = sync.update_page(release_key)
            print(f"ok=True\nurl={url}\nmsg=Страница успешно создана")
            sys.exit(0)

        # 👤 РУЧНОЙ РЕЖИМ: если скрипт запущен просто `python bt3.py`
        project_key = select_project()
        sync = JiraConfluenceSync(config, project_key)

        release_key = input("Ключ релиза (например, HRPRELEASE-84627): ").strip().upper()
        if not release_key:
            return

        print(f"\n⏳ Анализ связей 'consists of' в {release_key} для проекта {project_key}...")
        url = sync.update_page(release_key)

        print(f"\n✅ Готово: {url}")
        if input("Открыть? (y/n): ").lower() in ['y', 'д']:
            webbrowser.open(url)

    except Exception as e:
        logging.error(f"Критическая ошибка: {e}")
        # Выдаем ошибку в формате, понятном боту
        print(f"ok=False\nurl=\nmsg={e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
