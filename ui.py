import customtkinter as ctk
import os
import sys
import logging
import threading
import time
import re
import json
import urllib3
import warnings
import operator
import requests
import subprocess
import ast
from datetime import datetime
from tkinter import messagebox
import webbrowser

from onboarding import show_onboarding_if_needed
from config import JiraConfig, CONFLUENCE_URL, CONFLUENCE_TOKEN, CONFLUENCE_SPACE_KEY, CONFLUENCE_PARENT_PAGE_TITLE, CONFLUENCE_TEMPLATE_PAGE_ID, TEAM_NAME
from service import JiraService
from history import OperationHistory
from lt import run_lt_check_with_target
from rqg import run_rqg_check
from release_pr_status import (
    collect_release_tasks_pr_status,
    format_release_tasks_pr_report,
)
from master_analyzer import MasterServicesAnalyzer, ConfluenceDeployPlanGenerator

# === ИМПОРТЫ ДЛЯ ИИ-АГЕНТА ===
from langchain_core.messages import BaseMessage, AIMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.tools import tool
from langchain_core.runnables import Runnable
from langgraph.graph import StateGraph, END
from typing import Any, List, TypedDict, Annotated, Sequence

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# Отключаем SSL-шум для Агента
warnings.filterwarnings('ignore')
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
for k in ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"]:
    os.environ.pop(k, None)


# ==========================================
# GIGACHAT CLIENT (Интегрирован для UI)
# ==========================================
GIGA_CONFIG = {
    "person_id_dev": "91ed8888-bff4-4d61-a72d-310db2eeaa37",
    "client_id": "fakeuser",
    "model": "GigaChat-2-Max",  # KEEPING GIGACHAT-2-MAX AS USER REQUESTED!
    "token_url_dev": "https://hr-ift.sberbank.ru/auth/realms/PAOSberbank/protocol/openid-connect/token",
    "api_url_dev": "https://hr-ift.sberbank.ru/api-web/neurosearchbar/api/v1/gigachat/completion"
}

class SberGigaChatHR(BaseChatModel):
    _access_token: str = None
    _token_expires_at: float = 0

    @property
    def _llm_type(self) -> str: return "sber-gigachat-hr"
    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable: return self

    def _get_access_token(self) -> str:
        username = os.getenv("GIGACHAT_USERNAME")
        password = os.getenv("GIGACHAT_PASSWORD")
        if not username or not password:
            raise ValueError("Нет GIGACHAT_USERNAME/PASSWORD в .env")

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "x-hrp-person-id": GIGA_CONFIG["person_id_dev"],
            "User-Agent": "insomnia/8.6.1", "Accept": "*/*"
        }
        payload = {"grant_type": "password", "username": username, "password": password, "client_id": GIGA_CONFIG["client_id"]}
        r = requests.post(GIGA_CONFIG["token_url_dev"], data=payload, headers=headers, verify=False, timeout=10)
        if r.status_code != 200: raise Exception(f"Token error: {r.status_code}")

        data = r.json()
        self._access_token = data.get("access_token")
        self._token_expires_at = time.time() + data.get("expires_in", 1800) - 60
        return self._access_token

    def _generate(self, messages: List[BaseMessage], stop: list | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        if not self._access_token or time.time() > self._token_expires_at: self._get_access_token()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "personId": GIGA_CONFIG["person_id_dev"],
        }
        formatted = []
        for m in messages:
            if m.type == "human": formatted.append({"role": "user", "content": m.content})
            elif m.type == "system": formatted.append({"role": "system", "content": m.content})
            elif m.type == "tool": formatted.append({"role": "user", "content": f"СИСТЕМНЫЙ ОТВЕТ ОТ ИНСТРУМЕНТА:\\n{m.content}"})
            else: formatted.append({"role": "assistant", "content": m.content})

        payload = {"model": GIGA_CONFIG["model"], "messages": formatted, "temperature": 0.01}
        r = requests.post(GIGA_CONFIG["api_url_dev"], headers=headers, json=payload, verify=False, timeout=150)
        if r.status_code != 200: return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"API Error {r.status_code}"))])

        content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        if messages and messages[-1].type == "tool":
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content.strip()))])

        tool_calls = []

        # 1. Попытка распарсить сложный JSON-массив ({"commands": [...]})
        if "commands" in content or "tool" in content:
            try:
                json_match = re.search(r"```json\\n(.*?)\\n```", content, re.DOTALL)
                json_str = json_match.group(1) if json_match else content
                parsed_data = json.loads(json_str.replace("'", '"'))

                if isinstance(parsed_data, dict) and "commands" in parsed_data:
                    for cmd in parsed_data["commands"]:
                        name = cmd.get("name") or cmd.get("tool")
                        cmd_args = cmd.get("args", cmd.get("arguments", {}))
                        if name:
                            tool_calls.append({"name": name, "args": cmd_args, "id": os.urandom(8).hex()})
                    content = "" # Убираем JSON из текста
            except Exception:
                pass

        # 2. Старый надежный метод для плоских словариков (если первый не сработал)
        if not tool_calls and ("{" in content):
            matches = re.findall(r"\\{[^{}]+\\}", content)
            for match in matches:
                try:
                    action = ast.literal_eval(match)
                    if isinstance(action, dict):
                        if action.get("tool"):
                            name = action.pop("tool")
                            tool_calls.append({"name": name, "args": action, "id": os.urandom(8).hex()})
                            content = content.replace(match, "")
                        elif action.get("name") and isinstance(action.get("args"), dict):
                            tool_calls.append({"name": action["name"], "args": action["args"], "id": os.urandom(8).hex()})
                            content = content.replace(match, "")
                        elif action.get("name") and isinstance(action.get("arguments"), dict):
                            tool_calls.append({"name": action["name"], "args": action["arguments"], "id": os.urandom(8).hex()})
                            content = content.replace(match, "")
                except Exception:
                    pass

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content.strip(), tool_calls=tool_calls))])


# ==========================================
# ИИ-АГЕНТ ДЛЯ BLAST
# ==========================================
class BlastAIAssistant:
    def __init__(self, app_gui):
        self.app_gui = app_gui
        self.memory = []
        self.llm = SberGigaChatHR().bind_tools([])
        self._setup_graph()

    def _extract_tool_calls_from_text(self, content: str):
        """Fallback-парсер tool-команд из текстового ответа модели."""
        if not content:
            return [], ""

        tool_calls = []
        remaining = content

        def add_tool_call(name: str, args: dict):
            if not name:
                return
            safe_args = args if isinstance(args, dict) else {}
            tool_calls.append({
                "name": str(name),
                "args": safe_args,
                "id": os.urandom(8).hex(),
            })

        # Попытка 1: JSON-блок или объект целиком.
        json_candidates = []
        fenced = re.findall(r"```json\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
        if fenced:
            json_candidates.extend(fenced)
        json_candidates.append(content)

        for candidate in json_candidates:
            parsed = None
            try:
                parsed = json.loads(candidate)
            except Exception:
                try:
                    parsed = ast.literal_eval(candidate)
                except Exception:
                    parsed = None

            if not isinstance(parsed, dict):
                continue

            if parsed.get("tool"):
                args = {k: v for k, v in parsed.items() if k != "tool"}
                add_tool_call(parsed.get("tool"), args)
                remaining = remaining.replace(candidate, "").strip()
            elif parsed.get("name") and isinstance(parsed.get("args"), dict):
                add_tool_call(parsed.get("name"), parsed.get("args"))
                remaining = remaining.replace(candidate, "").strip()
            elif parsed.get("name") and isinstance(parsed.get("arguments"), dict):
                add_tool_call(parsed.get("name"), parsed.get("arguments"))
                remaining = remaining.replace(candidate, "").strip()
            elif isinstance(parsed.get("commands"), list):
                for cmd in parsed["commands"]:
                    if not isinstance(cmd, dict):
                        continue
                    name = cmd.get("tool") or cmd.get("name")
                    args = cmd.get("args", cmd.get("arguments", {}))
                    if name:
                        add_tool_call(name, args)
                remaining = remaining.replace(candidate, "").strip()

        # Попытка 2: отдельные словари внутри текста.
        if not tool_calls and "{" in content:
            matches = re.findall(r"\{[^{}]+\}", content)
            for match in matches:
                try:
                    action = ast.literal_eval(match)
                except Exception:
                    continue
                if not isinstance(action, dict):
                    continue

                if action.get("tool"):
                    args = {k: v for k, v in action.items() if k != "tool"}
                    add_tool_call(action.get("tool"), args)
                    remaining = remaining.replace(match, "")
                elif action.get("name") and isinstance(action.get("args"), dict):
                    add_tool_call(action.get("name"), action.get("args"))
                    remaining = remaining.replace(match, "")
                elif action.get("name") and isinstance(action.get("arguments"), dict):
                    add_tool_call(action.get("name"), action.get("arguments"))
                    remaining = remaining.replace(match, "")

        return tool_calls, remaining.strip()

    def _setup_graph(self):
        @tool("get_jira_status")
        def get_jira_status(issue_key: str) -> str:
            """
            Получить статус Jira-задачи/релиза по ключу.

            Args:
                issue_key: Jira key в формате PROJECT-123 (например, HRPRELEASE-111135).
            """
            issue_key = (issue_key or "").strip().upper()
            if not issue_key:
                return "Ошибка: не передан issue_key. Укажи ключ задачи в формате PROJECT-123."
            self.app_gui.append_ai_chat(f"🛠️ [Агент] Иду в Jira за задачей {issue_key}...\\n")
            try:
                issue = self.app_gui.jira_service.get_issue_details(issue_key)
                if not issue: return f"Задача {issue_key} не найдена."
                status = issue.get('fields', {}).get('status', {}).get('name', 'Unknown')
                summary = issue.get('fields', {}).get('summary', '')
                return f"Ключ: {issue_key}\\nНазвание: {summary}\\nСтатус: {status}"
            except Exception as e: return f"Ошибка Jira API: {e}"

        @tool("create_deploy_plan")
        def create_deploy_plan(issue_key: str) -> str:
            """Создает страницу Деплой-плана (Deploy Plan) в Confluence для релиза."""
            self.app_gui.append_ai_chat(f"🛠️ [Агент] Анализирую мастер-ветки и генерирую деплой-план для {issue_key}...\\n")
            if not self.app_gui.master_analyzer: return "Ошибка: Модуль MasterAnalyzer не инициализирован."
            try:
                analysis = self.app_gui.master_analyzer.analyze_release(issue_key)
                if not analysis['success']: return f"Ошибка анализа: {analysis['message']}"

                result = self.app_gui.master_analyzer.generate_deploy_plan(
                    analysis_result=analysis, space_key=CONFLUENCE_SPACE_KEY,
                    parent_page_title=CONFLUENCE_PARENT_PAGE_TITLE, team_name=TEAM_NAME
                )
                if result['success']: return f"Деплой-план УСПЕШНО СОЗДАН! Ссылка: {result['page_url']}"
                return f"Ошибка при создании в Confluence: {result.get('message')}"
            except Exception as e: return f"Критическая ошибка создания деплой-плана: {e}"

        @tool("create_business_requirements")
        def create_business_requirements(issue_key: str, project_key: str) -> str:
            """
            Создать/обновить страницу БТ/ФР в Confluence по релизу.

            Args:
                issue_key: Ключ релиза в Jira (например, HRPRELEASE-111135).
                project_key: Ключ целевого проекта (например, SFILE, HRM, HRC).

            Ошибки:
                Если не передан project_key или issue_key, инструмент вернет понятную ошибку
                и попросит пользователя уточнить данные.
            """
            issue_key = (issue_key or "").strip().upper()
            project_key = (project_key or "").strip().upper()
            if not issue_key:
                return "Ошибка: не передан issue_key релиза (пример: HRPRELEASE-111135)."
            if not project_key:
                return "Ошибка: не передан project_key (пример: SFILE)."
            self.app_gui.append_ai_chat(f"🛠️ [Агент] Собираю бизнес-требования для {issue_key} (проект {project_key})...\\n")
            try:
                script_path = os.path.join(os.path.dirname(__file__), "bt3.py")
                if not os.path.exists(script_path): return f"Ошибка: скрипт {script_path} не найден!"

                process = subprocess.run([sys.executable, script_path, issue_key, project_key], capture_output=True, text=True, check=False)
                output = process.stdout
                if "ok=True" in output:
                    url_match = re.search(r"url=(https?://[^\\s]+)", output)
                    url = url_match.group(1) if url_match else "Ссылка не найдена"
                    return f"Страница бизнес-требований успешно создана/обновлена! Ссылка: {url}"
                else: return f"Ошибка при создании бизнес-требований:\\nSTDOUT: {output}\\nSTDERR: {process.stderr}"
            except Exception as e: return f"Системная ошибка запуска скрипта БТ: {e}"

        @tool("check_lead_time")
        def check_lead_time(release_key: str) -> str:
            """
            Запускает проверку метрики Lead Time (LT) для ВСЕХ задач внутри указанного РЕЛИЗА.
            Используй этот инструмент, если пользователь спрашивает "какой LT", "проверь лид тайм", "посчитай LT" для релиза.
            Инструменту НУЖЕН ТОЛЬКО КЛЮЧ РЕЛИЗА (например, HRPRELEASE-1111).
            Не проси у пользователя ключи отдельных задач! Скрипт сам найдет все задачи.
            """
            release_key = (release_key or "").strip().upper()
            if not release_key:
                return "Ошибка: не передан release_key (пример: HRPRELEASE-111135)."
            self.app_gui.append_ai_chat(f"🛠️ [Агент] Запускаю проверку LT для релиза {release_key}...\\n")
            try:
                from lt import run_lt_check_with_target
                report = run_lt_check_with_target(release_key, 45)
                return f"Отчет по Lead Time для релиза {release_key} успешно сформирован:\\n\\n{report}"
            except Exception as e: return f"Ошибка проверки LT: {e}"

        @tool("check_rqg")
        def check_rqg(release_key: str, max_depth: int = 2, trigger_button: bool = True) -> str:
            """
            Выполнить RQG-проверку релиза.

            Args:
                release_key: Ключ релиза Jira (например, HRPRELEASE-111135).
                max_depth: Глубина обхода связанных задач (обычно 2).
                trigger_button: Нажимать ли Jira transition/button "RQG" перед проверкой.
            """
            release_key = (release_key or "").strip().upper()
            if not release_key:
                return "Ошибка: не передан release_key (пример: HRPRELEASE-111135)."
            self.app_gui.append_ai_chat(f"🛠️ [Агент] Запускаю RQG-проверки для релиза {release_key}...\\n")
            try:
                report = run_rqg_check(
                    self.app_gui.jira_service,
                    release_key,
                    max_depth=max_depth,
                    trigger_button=trigger_button,
                )
                return report
            except Exception as e:
                return f"Ошибка RQG-проверки: {e}"

        @tool("update_architecture_status")
        def update_architecture_status(release_key: str) -> str:
            """
            Запускает скрипт изменения статуса архитектуры (arch.py).
            Используй, если просят: "проставь архитектуру", "закрой архитектурные задачи", "арх не меняется".
            """
            release_key = (release_key or "").strip().upper()
            if not release_key:
                return "Ошибка: не передан release_key (пример: HRPRELEASE-111135)."
            self.app_gui.append_ai_chat(f"🛠️ [Агент] Меняю статус архитектуры для релиза {release_key}...\\n")
            try:
                script_path = os.path.join(os.path.dirname(__file__), "arch.py")
                if not os.path.exists(script_path): return "Ошибка: скрипт arch.py не найден."
                process = subprocess.run([sys.executable, script_path, release_key], capture_output=True, text=True, check=False)
                return f"Статус архитектуры обновлен!\\nРезультат: {process.stdout}"
            except Exception as e: return f"Ошибка обновления архитектуры: {e}"

        @tool("move_release_status")
        def move_release_status(issue_key: str, target_status: str) -> str:
            """
            Перевести Jira-задачу/релиз в целевой статус.

            Args:
                issue_key: Jira key, например HRPRELEASE-111135.
                target_status: Название статуса Jira (например, Ready for Prod).
            """
            issue_key = (issue_key or "").strip().upper()
            target_status = (target_status or "").strip()
            if not issue_key:
                return "Ошибка: не передан issue_key (пример: HRPRELEASE-111135)."
            if not target_status:
                return "Ошибка: не передан target_status (пример: Ready for Prod)."
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Перевожу {issue_key} в статус '{target_status}'...\\n"
            )
            try:
                ok, message = self.app_gui.jira_service.transition_issue(issue_key, target_status)
                if ok:
                    issue = self.app_gui.jira_service.get_issue_details(issue_key) or {}
                    status = issue.get("fields", {}).get("status", {}).get("name", "Unknown")
                    return f"✅ {message}\\nТекущий статус: {status}"
                return f"❌ {message}"
            except Exception as e:
                return f"Ошибка перевода статуса: {e}"

        @tool("run_release_pipeline")
        def run_release_pipeline(
            issue_key: str,
            project_key: str = "",
            target_lt: float | None = 45,
            create_bt: bool = False,
            create_deploy: bool = False,
        ) -> str:
            """
            Выполняет комплексную проверку релиза: статус, LT, master-check и опционально БТ/deploy-plan.
            Используй, когда пользователь просит полный прогон релиза.
            """
            issue_key = (issue_key or "").strip().upper()
            if not issue_key:
                return "Ошибка: не передан issue_key релиза (пример: HRPRELEASE-111135)."
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Запускаю полный релизный пайплайн для {issue_key}...\\n"
            )
            result_lines = [f"🚀 Релизный пайплайн: {issue_key}", "-" * 60]
            safe_project = (project_key or "").strip().upper()

            try:
                issue = self.app_gui.jira_service.get_issue_details(issue_key)
                if issue:
                    status = issue.get("fields", {}).get("status", {}).get("name", "Unknown")
                    summary = issue.get("fields", {}).get("summary", "")
                    result_lines.append(f"1) Jira статус: {status}")
                    if summary:
                        result_lines.append(f"   Название: {summary}")
                else:
                    result_lines.append("1) Jira статус: не удалось получить карточку релиза")
            except Exception as e:
                result_lines.append(f"1) Jira статус: ошибка ({e})")

            safe_target_lt = 45.0 if target_lt is None else float(target_lt)

            try:
                lt_report = run_lt_check_with_target(issue_key, safe_target_lt)
                result_lines.append(f"2) LT проверка: выполнена (target={safe_target_lt})")
                # Оставляем только самые полезные строки для чата.
                short_lt = []
                for line in lt_report.splitlines():
                    if "ПРЕВЫШЕНИЕ НОРМЫ" in line or "НОРМА СОБЛЮДЕНА" in line or "LT релиза" in line:
                        short_lt.append(line.strip())
                if short_lt:
                    result_lines.extend([f"   {line}" for line in short_lt[:4]])
            except Exception as e:
                result_lines.append(f"2) LT проверка: ошибка ({e})")

            try:
                rqg_report = run_rqg_check(
                    self.app_gui.jira_service,
                    issue_key,
                    max_depth=2,
                    trigger_button=True,
                )
                result_lines.append("3) RQG-проверка: выполнена")
                rqg_lines = []
                for line in rqg_report.splitlines():
                    if "Story проверено:" in line or "Пройдено:" in line or "Не пройдено:" in line:
                        rqg_lines.append(line.strip())
                if rqg_lines:
                    result_lines.extend([f"   {line}" for line in rqg_lines[:3]])
            except Exception as e:
                result_lines.append(f"3) RQG-проверка: ошибка ({e})")

            analysis = None
            try:
                if self.app_gui.master_analyzer:
                    analysis = self.app_gui.master_analyzer.analyze_release(issue_key)
                    if analysis.get("success"):
                        result_lines.append(
                            f"4) Master-check: OK, сервисов в master = {len(analysis.get('services', []))}"
                        )
                    else:
                        result_lines.append(
                            f"4) Master-check: ошибка анализа ({analysis.get('message', 'unknown')})"
                        )
                else:
                    result_lines.append("4) Master-check: модуль не инициализирован")
            except Exception as e:
                result_lines.append(f"4) Master-check: ошибка ({e})")

            bt_requested = str(create_bt).strip().lower() in ("1", "true", "yes", "y", "да")
            if bt_requested:
                if not safe_project:
                    result_lines.append("5) БТ: пропущено (нужен project_key)")
                else:
                    try:
                        bt_result = create_business_requirements.invoke(
                            {"issue_key": issue_key, "project_key": safe_project}
                        )
                        result_lines.append(f"5) БТ: {bt_result}")
                    except Exception as e:
                        result_lines.append(f"5) БТ: ошибка ({e})")

            deploy_requested = str(create_deploy).strip().lower() in ("1", "true", "yes", "y", "да")
            if deploy_requested:
                if not analysis or not analysis.get("success"):
                    result_lines.append("6) Deploy plan: пропущено (сначала нужен успешный master-check)")
                else:
                    try:
                        deploy = self.app_gui.master_analyzer.generate_deploy_plan(
                            analysis_result=analysis,
                            space_key=CONFLUENCE_SPACE_KEY,
                            parent_page_title=CONFLUENCE_PARENT_PAGE_TITLE,
                            team_name=TEAM_NAME,
                        )
                        if deploy.get("success"):
                            result_lines.append(f"6) Deploy plan: создан ({deploy.get('page_url')})")
                        else:
                            result_lines.append(
                                f"6) Deploy plan: ошибка ({deploy.get('message', 'unknown')})"
                            )
                    except Exception as e:
                        result_lines.append(f"6) Deploy plan: ошибка ({e})")

            result_lines.append("-" * 60)
            result_lines.append("Готово. Пришли команду для следующего шага (например: 'переведи в Ready for Prod').")
            return "\n".join(result_lines)

        @tool("check_release_tasks_pr_status")
        def check_release_tasks_pr_status(release_key: str) -> str:
            """
            Запускает проверку Story/Bug задач релиза и статусов связанных PR.

            Args:
                release_key: Ключ релиза в Jira (например, HRPRELEASE-111135).

            Поведение:
                Проверка выполняется в отдельном потоке, прогресс виден во вкладке Операции,
                итоговый отчет публикуется в AI-чат автоматически.
            """
            release_key = (release_key or "").strip().upper()
            if not release_key:
                return "Ошибка: не передан release_key (пример: HRPRELEASE-111135)."
            return self.app_gui.start_release_pr_status_check(
                release_key=release_key,
                announce_in_chat=True,
            )

        @tool("link_tasks_to_release")
        def link_tasks_to_release(release_key: str, fix_version: str) -> str:
            """
            Привязать Story/Bug задачи выбранной версии к релизу.

            Args:
                release_key: Ключ релиза Jira (например, HRPRELEASE-113937).
                fix_version: Значение fixVersion (например, HM-REL-05-03-2026).
            """
            release_key = (release_key or "").strip().upper()
            fix_version = (fix_version or "").strip()
            if not release_key:
                return "Ошибка: не передан release_key (пример: HRPRELEASE-113937)."
            if not fix_version:
                return "Ошибка: не передан fix_version (пример: HM-REL-05-03-2026)."
            return self.app_gui.start_link_issues_from_ai(
                release_key=release_key,
                fix_version=fix_version,
            )

        @tool("link_issues_by_fix_version")
        def link_issues_by_fix_version(release_key: str, fix_version: str) -> str:
            """
            Алиас инструмента привязки задач по fixVersion к релизу.
            Нужен для совместимости с вариантами названия в промптах.
            """
            return link_tasks_to_release.invoke(
                {"release_key": release_key, "fix_version": fix_version}
            )

        self.tools_map = {
            "get_jira_status": get_jira_status,
            "create_deploy_plan": create_deploy_plan,
            "create_business_requirements": create_business_requirements,
            "check_lead_time": check_lead_time,
            "check_rqg": check_rqg,
            "update_architecture_status": update_architecture_status,
            "move_release_status": move_release_status,
            "run_release_pipeline": run_release_pipeline,
            "check_release_tasks_pr_status": check_release_tasks_pr_status,
            "link_tasks_to_release": link_tasks_to_release,
            "link_issues_by_fix_version": link_issues_by_fix_version,
        }

        class AgentState(TypedDict):
            messages: Annotated[Sequence[BaseMessage], operator.add]

        def call_model(state: AgentState):
            msgs = list(state["messages"])
            if msgs[-1].type == "tool":
                sys_msg = SystemMessage(content="Расскажи пользователю результат действия дружелюбно по-русски. Выведи ссылки, если есть.")
                return {"messages": [self.llm.invoke([sys_msg] + msgs)]}

            sys_msg = SystemMessage(content=(
                "Ты DevOps AI-помощник в приложении Blast. "
                "Твоя задача — надежно вызывать инструменты и возвращать пользователю полезный итог.\\n\\n"
                "ДОСТУПНЫЕ ИНСТРУМЕНТЫ:\\n"
                "1) get_jira_status(issue_key) — статус Jira задачи/релиза.\\n"
                "2) create_deploy_plan(issue_key) — создать deploy plan в Confluence.\\n"
                "3) create_business_requirements(issue_key, project_key) — создать/обновить БТ/ФР.\\n"
                "4) check_lead_time(release_key) — LT по релизу.\\n"
                "5) check_rqg(release_key, max_depth=2, trigger_button=True) — RQG-проверка.\\n"
                "6) update_architecture_status(release_key) — запуск скрипта арх-статуса.\\n"
                "7) move_release_status(issue_key, target_status) — перевод релиза в статус.\\n"
                "8) run_release_pipeline(issue_key, project_key='', target_lt=45, create_bt=False, create_deploy=False).\\n"
                "9) check_release_tasks_pr_status(release_key) — Story/Bug + PR статусы (Open/Merged).\\n\\n"
                "10) link_tasks_to_release(release_key, fix_version) — привязка задач fixVersion к релизу.\\n\\n"
                "11) link_issues_by_fix_version(release_key, fix_version) — алиас привязки задач к релизу.\\n\\n"
                "ПРАВИЛА ВЫЗОВА:\\n"
                "- Если пользователь просит действие, сначала вызови соответствующий инструмент, не выдумывай результат.\\n"
                "- При нехватке обязательных аргументов ЗАДАЙ УТОЧНЯЮЩИЙ ВОПРОС и не вызывай инструмент.\\n"
                "- Важные обязательные параметры: "
                "issue_key/release_key в формате PROJECT-123, "
                "project_key (для create_business_requirements), "
                "target_status (для move_release_status), "
                "fix_version (для link_tasks_to_release).\\n"
                "- Если пользователь пишет 'собери задачи с версией X в релиз Y', вызови link_tasks_to_release(release_key=Y, fix_version=X).\\n"
                "- Допустим также алиас link_issues_by_fix_version с теми же аргументами.\\n"
                "- Если пользователь просит несколько действий, верни несколько отдельных JSON-словарей вызова инструментов подряд.\\n"
                "- Не используй массив commands, не группируй вызовы в один объект.\\n"
                "- После ToolMessage ответь кратко и структурно: что выполнено, итог, ошибки/что нужно уточнить."
            ))

            m = re.search(r"([A-Z]+-\\d+)", msgs[-1].content or "", re.IGNORECASE)
            if m and msgs[-1].type == "human":
                msgs[-1] = HumanMessage(content=f"{msgs[-1].content}\\n[Контекст: упоминается релиз {m.group(1).upper()}]")

            response = self.llm.invoke([sys_msg] + msgs)
            fallback_calls, cleaned_content = self._extract_tool_calls_from_text(response.content or "")
            if not getattr(response, "tool_calls", None) and fallback_calls:
                response = AIMessage(content=cleaned_content, tool_calls=fallback_calls)
            return {"messages": [response]}

        def execute_tools(state: AgentState):
            results = []
            last_message = state["messages"][-1]
            tool_calls = getattr(last_message, "tool_calls", []) or []
            if not tool_calls and getattr(last_message, "content", None):
                tool_calls, _ = self._extract_tool_calls_from_text(last_message.content)

            for tc in tool_calls:
                name = tc.get("name")
                args = tc.get("args") or tc.get("arguments") or {}
                if name in self.tools_map:
                    try:
                        out = self.tools_map[name].invoke(args)
                        results.append(ToolMessage(content=str(out), name=name, tool_call_id=tc.get("id")))
                    except Exception as e:
                        results.append(ToolMessage(content=f"Ошибка выполнения {name}: {e}", name=name, tool_call_id=tc.get("id")))
                else:
                    results.append(ToolMessage(content="Неизвестный инструмент", name=str(name), tool_call_id=tc.get("id")))
            return {"messages": results}

        workflow = StateGraph(AgentState)
        workflow.add_node("agent", call_model)
        workflow.add_node("action", execute_tools)
        workflow.set_entry_point("agent")
        def should_continue(state: AgentState):
            last_message = state["messages"][-1]
            if getattr(last_message, "tool_calls", None):
                return "continue"
            if getattr(last_message, "content", None):
                parsed_calls, _ = self._extract_tool_calls_from_text(last_message.content)
                if parsed_calls:
                    return "continue"
            return "end"

        workflow.add_conditional_edges("agent", should_continue, {"continue": "action", "end": END})
        workflow.add_edge("action", "agent")
        self.app_graph = workflow.compile()

    def _handle_direct_commands(self, text: str) -> bool:
        """Надежные прямые команды (без LLM), чтобы критичные операции не терялись."""
        raw = (text or "").strip()
        lowered = raw.lower()

        release_match = re.search(r"(HRPRELEASE-\d+)", raw, re.IGNORECASE)
        version_match = re.search(
            r"(?:верси\w*|fix\s*version|fixversion)\s*[:=]?\s*([A-Z0-9._\-]+)",
            raw,
            re.IGNORECASE,
        )

        link_intent = any(
            phrase in lowered
            for phrase in (
                "собери задачи",
                "привяжи задачи",
                "привязать задачи",
                "линкуй задачи",
                "link tasks",
            )
        )
        if link_intent and release_match and version_match:
            release_key = release_match.group(1).upper()
            fix_version = version_match.group(1).strip()
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Прямая команда: привязка задач {fix_version} -> {release_key}\n"
            )
            result = self.app_gui.start_link_issues_from_ai(
                release_key=release_key,
                fix_version=fix_version,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        return False

    def process_message(self, text: str):
        
        if self._handle_direct_commands(text):
            self.app_gui.set_ai_input_state("normal")
            return

        self.memory.append(HumanMessage(content=text))
        try:
            for out in self.app_graph.stream({"messages": self.memory}):
                if "agent" in out:
                    msg = out["agent"]["messages"][-1]
                    self.memory.append(msg)
                    tool_calls = getattr(msg, "tool_calls", []) or []
                    if not tool_calls and (msg.content or "").strip():
                        tool_calls, _ = self._extract_tool_calls_from_text(msg.content)
                    if (msg.content or "").strip() and not tool_calls:
                        self.app_gui.append_ai_chat(f"🤖 Blast AI: {msg.content}\\n\\n")
                if "action" in out:
                    self.memory.extend(out["action"]["messages"])
        except Exception as e:
            self.app_gui.append_ai_chat(f"⚠️ Ошибка ИИ: {e}\\n\\n")
        finally:
            self.app_gui.set_ai_input_state("normal")


# ==========================================
# ОСНОВНОЕ ПРИЛОЖЕНИЕ (Blast)
# ==========================================
class ModernJiraApp(ctk.CTk):
    """Современное приложение для работы с Jira на CustomTkinter"""

    def __init__(self):
        super().__init__()

        self.title("Blast - Jira Automation Tool")
        self.geometry("1200x800")

        self.config_dir = os.path.join(os.path.expanduser('~'), '.jira_tool')
        self.config_path = os.path.join(self.config_dir, 'config.json')
        self.history_path = os.path.join(self.config_dir, 'history.json')

        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir)

        self.config = JiraConfig()
        self.jira_service = JiraService(self.config)
        self.history = OperationHistory()
        if os.path.exists(self.history_path):
            self.history.load_from_file(self.history_path)

        self.cancel_operation = False
        self.current_operation = None
        self.current_analysis = None

        # Инициализация Master Analyzer
        try:
            self.confluence_generator = ConfluenceDeployPlanGenerator(
                confluence_url=CONFLUENCE_URL,
                confluence_token=CONFLUENCE_TOKEN,
                template_page_id=CONFLUENCE_TEMPLATE_PAGE_ID
            )
            self.master_analyzer = MasterServicesAnalyzer(
                jira_service=self.jira_service,
                confluence_generator=self.confluence_generator
            )
        except Exception as e:
            logging.error(f"Ошибка инициализации Confluence: {e}")
            self.confluence_generator = None
            self.master_analyzer = None

        # Инициализация AI-Помощника
        self.ai_assistant = BlastAIAssistant(self)

        self.setup_logging()
        self.create_widgets()
        self.after(100, self.check_connection)
        self.after(200, lambda: show_onboarding_if_needed(self))

    def setup_logging(self):
        """Настройка логирования"""
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        log_file = os.path.join(self.config_dir, 'app.log')

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(self.__class__.__name__)

    def create_widgets(self):
        """Создание виджетов интерфейса"""
        # Боковая панель
        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        try:
            from PIL import Image
            logo_path = os.path.join(os.path.dirname(__file__), 'logo.png')
            if os.path.exists(logo_path):
                logo_image = Image.open(logo_path)
                logo_image = logo_image.resize((120, 120), Image.Resampling.LANCZOS)
                logo_ctk = ctk.CTkImage(light_image=logo_image, dark_image=logo_image, size=(120, 120))
                logo_label = ctk.CTkLabel(self.sidebar, image=logo_ctk, text="")
                logo_label.pack(pady=(20, 10))
        except Exception as e:
            self.logger.warning(f"Не удалось загрузить логотип: {e}")

        guide_btn = ctk.CTkButton(
            self.sidebar,
            text="📚 Гайд",
            command=self.show_onboarding_manual,
            width=180,
            height=40,
            font=("Arial", 14, "bold"),
            fg_color="#2196F3",
            hover_color="#1976D2"
        )
        guide_btn.pack(pady=(10, 20), padx=10)

        ctk.CTkLabel(self.sidebar, text="⚡ Blast ⚡", font=ctk.CTkFont(size=28, weight="bold")).pack(pady=(30, 5))
        ctk.CTkLabel(self.sidebar, text="v2.3 + AI", font=ctk.CTkFont(size=12), text_color="gray").pack(pady=(0, 30))

        # Кнопки навигации
        self.nav_btn_operations = ctk.CTkButton(
            self.sidebar, text="⚡ Операции", command=self.show_operations_tab,
            font=ctk.CTkFont(size=14), height=45
        )
        self.nav_btn_operations.pack(pady=10, padx=20, fill="x")

        # === КНОПКА ИИ ===
        self.nav_btn_ai = ctk.CTkButton(
            self.sidebar, text="🤖 AI Помощник", command=self.show_ai_tab,
            font=ctk.CTkFont(size=14, weight="bold"), fg_color="#673AB7", hover_color="#512DA8", height=45
        )
        self.nav_btn_ai.pack(pady=10, padx=20, fill="x")

        self.nav_btn_history = ctk.CTkButton(
            self.sidebar, text="📋 История", command=self.show_history_tab,
            font=ctk.CTkFont(size=14), height=45
        )
        self.nav_btn_history.pack(pady=10, padx=20, fill="x")

        self.nav_btn_settings = ctk.CTkButton(
            self.sidebar, text="⚙️ Настройки", command=self.show_settings_tab,
            font=ctk.CTkFont(size=14), height=45
        )
        self.nav_btn_settings.pack(pady=10, padx=20, fill="x")

        self.nav_btn_logs = ctk.CTkButton(
            self.sidebar, text="📄 Логи", command=self.show_logs_tab,
            font=ctk.CTkFont(size=14), height=45
        )
        self.nav_btn_logs.pack(pady=10, padx=20, fill="x")

        self.connection_label = ctk.CTkLabel(
            self.sidebar, text="● Проверка...",
            font=ctk.CTkFont(size=12), text_color="orange"
        )
        self.connection_label.pack(side="bottom", pady=20)

        # Основная область
        self.main_content = ctk.CTkFrame(self, corner_radius=0)
        self.main_content.pack(side="right", fill="both", expand=True)

        self.create_operations_tab()
        self.create_ai_tab()
        self.create_history_tab()
        self.create_settings_tab()
        self.create_logs_tab()
        self.show_operations_tab()

    # === ИНТЕРФЕЙС ВКЛАДКИ ИИ ===
    def create_ai_tab(self):
        """Вкладка ИИ-Помощника"""
        self.ai_tab = ctk.CTkFrame(self.main_content)

        header = ctk.CTkFrame(self.ai_tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 10))
        ctk.CTkLabel(header, text="Умный помощник", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")

        # Окно чата
        self.ai_chat_display = ctk.CTkTextbox(self.ai_tab, font=ctk.CTkFont(size=14), wrap="word", state="disabled")
        self.ai_chat_display.pack(fill="both", expand=True, padx=20, pady=10)
        self.ai_chat_display.tag_config("ai_user", foreground="#0D47A1")
        self.ai_chat_display.tag_config("ai_bot", foreground="#1B5E20")
        self.ai_chat_display.tag_config("ai_tool", foreground="#6A1B9A")
        self.ai_chat_display.tag_config("ai_error", foreground="#B71C1C")
        self.ai_chat_display.tag_config("ai_default", foreground="#263238")

        # Приветственное сообщение
        self.append_ai_chat(
            "🤖 Blast AI: Готов к работе с релизом.\\n"
            "Доступно: проверка статуса, LT, RQG, master-check, Story/Bug+PR отчет, создание БТ, деплой-плана и перевод статуса релиза.\\n"
            "Примеры команд:\\n"
            "• Проверь статус HRPRELEASE-111135\\n"
            "• Проверь RQG для HRPRELEASE-111135\\n"
            "• Проверь задачи и PR для HRPRELEASE-111135\\n"
            "• Запусти полный пайплайн для HRPRELEASE-111135, проект SFILE\\n"
            "• Переведи HRPRELEASE-111135 в Ready for Prod\\n\\n"
        )

        quick_frame = ctk.CTkFrame(self.ai_tab)
        quick_frame.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(
            quick_frame,
            text="Быстрые сценарии:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left", padx=(10, 8), pady=8)

        ctk.CTkButton(
            quick_frame,
            text="Статус релиза",
            width=130,
            command=lambda: self.send_ai_quick_command("Проверь статус HRPRELEASE-"),
        ).pack(side="left", padx=5, pady=6)

        ctk.CTkButton(
            quick_frame,
            text="Полный пайплайн",
            width=150,
            fg_color="#673AB7",
            hover_color="#512DA8",
            command=lambda: self.send_ai_quick_command(
                "Запусти полный пайплайн для HRPRELEASE-, проект SFILE, create_bt=true, create_deploy=true"
            ),
        ).pack(side="left", padx=5, pady=6)

        ctk.CTkButton(
            quick_frame,
            text="Сдвинуть статус",
            width=140,
            command=lambda: self.send_ai_quick_command(
                "Переведи HRPRELEASE- в Ready for Prod"
            ),
        ).pack(side="left", padx=5, pady=6)

        ctk.CTkButton(
            quick_frame,
            text="Задачи + PR",
            width=130,
            fg_color="#1976D2",
            hover_color="#1565C0",
            command=lambda: self.send_ai_quick_command(
                "Проверь задачи и PR для HRPRELEASE-"
            ),
        ).pack(side="left", padx=5, pady=6)

        # Ввод
        input_frame = ctk.CTkFrame(self.ai_tab, fg_color="transparent")
        input_frame.pack(fill="x", padx=20, pady=(0, 20))

        self.ai_input = ctk.CTkEntry(
            input_frame,
            font=ctk.CTkFont(size=14),
            placeholder_text="Напр: Запусти полный пайплайн для HRPRELEASE-113300, project SFILE",
        )
        self.ai_input.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.ai_input.bind("<Return>", self.send_ai_message)

        self.ai_send_btn = ctk.CTkButton(input_frame, text="Отправить 🚀", width=120, height=40, font=ctk.CTkFont(weight="bold"), command=self.send_ai_message)
        self.ai_send_btn.pack(side="right")

    def append_ai_chat(self, text):
        def update():
            self.ai_chat_display.configure(state="normal")
            tag = "ai_default"
            plain = (text or "").lstrip()
            if plain.startswith("👤"):
                tag = "ai_user"
            elif plain.startswith("🤖"):
                tag = "ai_bot"
            elif plain.startswith("🛠️"):
                tag = "ai_tool"
            elif plain.startswith("⚠️") or plain.startswith("❌"):
                tag = "ai_error"
            self.ai_chat_display.insert("end", text, tag)
            self.ai_chat_display.see("end")
            self.ai_chat_display.configure(state="disabled")
        self.after(0, update)

    def set_ai_input_state(self, state):
        self.after(0, lambda: self.ai_send_btn.configure(state=state))

    def send_ai_message(self, event=None):
        if self.ai_send_btn.cget("state") == "disabled": return
        text = self.ai_input.get().strip()
        if not text: return
        self.ai_input.delete(0, "end")
        self.append_ai_chat(f"👤 Вы: {text}\\n")
        self.set_ai_input_state("disabled")
        threading.Thread(target=self.ai_assistant.process_message, args=(text,), daemon=True).start()

    def send_ai_quick_command(self, command: str):
        """Быстрая отправка предзаполненной команды в AI-чат"""
        self.ai_input.delete(0, "end")
        self.ai_input.insert(0, command)
        self.ai_input.focus()

    def create_operations_tab(self):
        """Вкладка операций"""
        self.operations_tab = ctk.CTkFrame(self.main_content)

        header = ctk.CTkFrame(self.operations_tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 10))
        ctk.CTkLabel(header, text="Операции с релизами", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")

        self.operations_tabs = ctk.CTkTabview(self.operations_tab)
        self.operations_tabs.pack(fill="both", expand=True, padx=20, pady=10)
        actions_tab = self.operations_tabs.add("Операции")
        monitor_tab = self.operations_tabs.add("Мониторинг")
        self.operations_tabs.set("Операции")

        input_frame = ctk.CTkFrame(actions_tab)
        input_frame.pack(fill="x", padx=4, pady=8)
        input_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(input_frame, text="Ключ релиза:", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=10, pady=10, sticky="e")
        self.release_entry = ctk.CTkEntry(
            input_frame,
            width=320,
            font=ctk.CTkFont(size=14),
            placeholder_text="HRPRELEASE-123456",
        )
        self.release_entry.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        ctk.CTkLabel(input_frame, text="Fix Version:", font=ctk.CTkFont(size=14, weight="bold")).grid(row=1, column=0, padx=10, pady=10, sticky="e")
        self.version_entry = ctk.CTkEntry(
            input_frame,
            width=320,
            font=ctk.CTkFont(size=14),
            placeholder_text="Minor-2026-03-10",
        )
        self.version_entry.grid(row=1, column=1, padx=10, pady=10, sticky="ew")

        ctk.CTkLabel(input_frame, text="Целевой LT (дни):", font=ctk.CTkFont(size=14, weight="bold")).grid(row=2, column=0, padx=10, pady=10, sticky="e")
        self.target_lt_entry = ctk.CTkEntry(input_frame, width=100, font=ctk.CTkFont(size=14))
        self.target_lt_entry.insert(0, "45")
        self.target_lt_entry.grid(row=2, column=1, padx=10, pady=10, sticky="w")

        status_panel = ctk.CTkFrame(actions_tab)
        status_panel.pack(fill="x", padx=4, pady=8)
        status_panel.grid_columnconfigure(1, weight=1)
        status_panel.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            status_panel,
            text="Статус релиза:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=10, sticky="e")

        self.release_status_value = ctk.CTkLabel(
            status_panel,
            text="не загружен",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#616161",
        )
        self.release_status_value.grid(row=0, column=1, padx=10, pady=10, sticky="w")

        self.refresh_status_btn = ctk.CTkButton(
            status_panel,
            text="Обновить статус",
            width=140,
            command=self.refresh_release_status,
        )
        self.refresh_status_btn.grid(row=0, column=2, padx=8, pady=10, sticky="w")

        ctk.CTkLabel(
            status_panel,
            text="Новый статус:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=1, column=0, padx=10, pady=10, sticky="e")

        self.target_status_entry = ctk.CTkEntry(
            status_panel,
            width=260,
            font=ctk.CTkFont(size=13),
            placeholder_text="Напр: Ready for Prod",
        )
        self.target_status_entry.grid(row=1, column=1, padx=10, pady=10, sticky="w")

        self.move_status_btn = ctk.CTkButton(
            status_panel,
            text="Сдвинуть статус",
            width=140,
            fg_color="#00796B",
            hover_color="#00695C",
            command=self.move_release_status_manual,
        )
        self.move_status_btn.grid(row=1, column=2, padx=8, pady=10, sticky="w")

        self.ai_pipeline_btn = ctk.CTkButton(
            status_panel,
            text="🤖 Полный пайплайн",
            width=170,
            fg_color="#512DA8",
            hover_color="#4527A0",
            command=self.run_ai_release_pipeline,
        )
        self.ai_pipeline_btn.grid(row=0, column=3, rowspan=2, padx=8, pady=10, sticky="ew")

        options_frame = ctk.CTkFrame(actions_tab)
        options_frame.pack(fill="x", padx=4, pady=8)

        self.dry_run_var = ctk.BooleanVar(value=False)
        self.dry_run_check = ctk.CTkCheckBox(options_frame, text="Тестовый прогон", variable=self.dry_run_var, font=ctk.CTkFont(size=13))
        self.dry_run_check.pack(side="left", padx=10)

        self.parallel_var = ctk.BooleanVar(value=True)
        self.parallel_check = ctk.CTkCheckBox(options_frame, text="Параллельная обработка", variable=self.parallel_var, font=ctk.CTkFont(size=13))
        self.parallel_check.pack(side="left", padx=10)

        buttons_frame = ctk.CTkFrame(actions_tab)
        buttons_frame.pack(fill="x", padx=4, pady=8)

        self.link_btn = ctk.CTkButton(buttons_frame, text="🔗 Привязать задачи", command=self.link_issues, font=ctk.CTkFont(size=14), height=40)
        self.link_btn.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        self.cleanup_btn = ctk.CTkButton(buttons_frame, text="🧹 Очистить связи", command=self.cleanup_issues, font=ctk.CTkFont(size=14), height=40)
        self.cleanup_btn.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        self.remove_all_btn = ctk.CTkButton(buttons_frame, text="⚠️ Удалить все связи", command=self.remove_all_issues, font=ctk.CTkFont(size=14), height=40, fg_color="#d13438", hover_color="#a80000")
        self.remove_all_btn.grid(row=0, column=2, padx=5, pady=5, sticky="ew")

        self.lt_btn = ctk.CTkButton(buttons_frame, text="📊 Проверка LT", command=self.run_lt_check, font=ctk.CTkFont(size=14), height=40)
        self.lt_btn.grid(row=1, column=0, padx=5, pady=5, sticky="ew")

        self.rqg_btn = ctk.CTkButton(
            buttons_frame,
            text="🛡 Проверка RQG",
            command=self.run_rqg_check,
            font=ctk.CTkFont(size=14),
            height=40,
            fg_color="#5C6BC0",
            hover_color="#3F51B5",
        )
        self.rqg_btn.grid(row=2, column=0, padx=5, pady=5, sticky="ew")

        self.pr_status_btn = ctk.CTkButton(
            buttons_frame,
            text="🔍 Задачи + PR",
            command=self.run_release_pr_status_ui,
            font=ctk.CTkFont(size=14),
            height=40,
            fg_color="#1976D2",
            hover_color="#1565C0",
        )
        self.pr_status_btn.grid(row=2, column=1, padx=5, pady=5, sticky="ew")

        self.master_btn = ctk.CTkButton(buttons_frame, text="🔀 Мастер-ветки", command=self.analyze_master_services, font=ctk.CTkFont(size=14), height=40, fg_color="#4CAF50", hover_color="#45a049")
        self.master_btn.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        self.deploy_btn = ctk.CTkButton(buttons_frame, text="📝 Деплой-план", command=self.create_deploy_plan, font=ctk.CTkFont(size=14), height=40, fg_color="#FF9800", hover_color="#F57C00", state="disabled")
        self.deploy_btn.grid(row=1, column=2, padx=5, pady=5, sticky="ew")

        self.cancel_btn = ctk.CTkButton(buttons_frame, text="❌ Отменить", command=self.cancel_current_operation, font=ctk.CTkFont(size=14), height=40, state="disabled")
        self.cancel_btn.grid(row=3, column=0, columnspan=4, padx=5, pady=5, sticky="ew")

        for i in range(4):
            buttons_frame.columnconfigure(i, weight=1)

        # Информация о Confluence
        if CONFLUENCE_SPACE_KEY and CONFLUENCE_PARENT_PAGE_TITLE:
            info_label = ctk.CTkLabel(
                actions_tab,
                text=f"Confluence: {CONFLUENCE_SPACE_KEY}/{CONFLUENCE_PARENT_PAGE_TITLE} | Команда: {TEAM_NAME}",
                font=ctk.CTkFont(size=10),
                text_color="gray"
            )
            info_label.pack(pady=5)

        progress_frame = ctk.CTkFrame(monitor_tab)
        progress_frame.pack(fill="x", padx=4, pady=8)

        self.progress_label = ctk.CTkLabel(
            progress_frame,
            text="Ожидание запуска...",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#616161",
        )
        self.progress_label.pack(pady=5)

        self.progress_bar = ctk.CTkProgressBar(progress_frame, width=600)
        self.progress_bar.pack(pady=5)
        self.progress_bar.set(0)

        self.details_label = ctk.CTkLabel(progress_frame, text="", font=ctk.CTkFont(size=12), text_color="gray")
        self.details_label.pack(pady=5)

        results_frame = ctk.CTkFrame(monitor_tab)
        results_frame.pack(fill="both", expand=True, padx=4, pady=8)

        ctk.CTkLabel(results_frame, text="Результаты:", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=10, pady=5)

        self.results_text = ctk.CTkTextbox(results_frame, font=ctk.CTkFont(family="Consolas", size=11), wrap="word")
        self.results_text.pack(fill="both", expand=True, padx=10, pady=5)
        self.results_text.tag_config("success", foreground="#2E7D32")
        self.results_text.tag_config("error", foreground="#C62828")
        self.results_text.tag_config("warning", foreground="#EF6C00")
        self.results_text.tag_config("info", foreground="#1E3A8A")

    def show_onboarding_manual(self):
        """Показать онбординг вручную"""
        from onboarding import OnboardingWindow
        OnboardingWindow(self)

    def create_history_tab(self):
        """Вкладка истории"""
        self.history_tab = ctk.CTkFrame(self.main_content)

        header = ctk.CTkFrame(self.history_tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=20)

        ctk.CTkLabel(header, text="История операций", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="🔄 Обновить", command=self.refresh_history, font=ctk.CTkFont(size=14)).pack(side="right", padx=5)
        ctk.CTkButton(header, text="🗑️ Очистить", command=self.clear_history, font=ctk.CTkFont(size=14)).pack(side="right", padx=5)

        self.history_text = ctk.CTkTextbox(self.history_tab, font=ctk.CTkFont(family="Consolas", size=11))
        self.history_text.pack(fill="both", expand=True, padx=20, pady=10)

    def create_settings_tab(self):
        """Вкладка настроек"""
        self.settings_tab = ctk.CTkFrame(self.main_content)

        ctk.CTkLabel(self.settings_tab, text="Настройки подключения", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=20)

        settings_form = ctk.CTkFrame(self.settings_tab)
        settings_form.pack(fill="x", padx=50, pady=20)

        ctk.CTkLabel(settings_form, text="URL Jira:", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=10, pady=15, sticky="e")
        self.url_entry = ctk.CTkEntry(settings_form, width=400, font=ctk.CTkFont(size=13))
        self.url_entry.insert(0, self.config.url)
        self.url_entry.grid(row=0, column=1, padx=10, pady=15)

        ctk.CTkLabel(settings_form, text="API Token:", font=ctk.CTkFont(size=14, weight="bold")).grid(row=1, column=0, padx=10, pady=15, sticky="e")
        self.token_entry = ctk.CTkEntry(settings_form, width=400, show="*", font=ctk.CTkFont(size=13))
        self.token_entry.insert(0, self.config.token)
        self.token_entry.grid(row=1, column=1, padx=10, pady=15)

        self.ssl_var = ctk.BooleanVar(value=self.config.verify_ssl)
        self.ssl_check = ctk.CTkCheckBox(settings_form, text="Проверять SSL сертификат", variable=self.ssl_var, font=ctk.CTkFont(size=13))
        self.ssl_check.grid(row=2, column=1, padx=10, pady=15, sticky="w")

        buttons = ctk.CTkFrame(self.settings_tab)
        buttons.pack(pady=20)

        ctk.CTkButton(buttons, text="💾 Сохранить", command=self.save_settings, font=ctk.CTkFont(size=14), width=150, height=40).pack(side="left", padx=10)
        ctk.CTkButton(buttons, text="🔌 Проверить подключение", command=self.test_connection, font=ctk.CTkFont(size=14), width=200, height=40).pack(side="left", padx=10)

        info_frame = ctk.CTkFrame(self.settings_tab)
        info_frame.pack(fill="x", padx=50, pady=20)

        info_text = """Blast v2.3

Jira Automation Tool + Confluence Deploy Plans

Возможности:
• Массовая привязка задач к релизам
• Умная очистка связей
• Параллельная обработка
• Проверка LT метрики
• Анализ мастер-веток (PR → Services)
• Генерация деплой-планов в Confluence
• История операций
• Детальное логирование"""



        ctk.CTkLabel(info_frame, text=info_text, font=ctk.CTkFont(size=12), justify="left").pack(padx=20, pady=20)

    def create_logs_tab(self):
        """Вкладка логов"""
        self.logs_tab = ctk.CTkFrame(self.main_content)

        header = ctk.CTkFrame(self.logs_tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=20)

        ctk.CTkLabel(header, text="Логи приложения", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="🔄 Обновить", command=self.refresh_logs, font=ctk.CTkFont(size=14)).pack(side="right", padx=5)
        ctk.CTkButton(header, text="🗑️ Очистить", command=self.clear_logs, font=ctk.CTkFont(size=14)).pack(side="right", padx=5)

        self.log_text = ctk.CTkTextbox(self.logs_tab, font=ctk.CTkFont(family="Consolas", size=10))
        self.log_text.pack(fill="both", expand=True, padx=20, pady=10)
        self.log_text.tag_config("log_error", foreground="#C62828")
        self.log_text.tag_config("log_warning", foreground="#EF6C00")
        self.log_text.tag_config("log_info", foreground="#1E3A8A")

    def hide_all_tabs(self):
        """Скрыть все вкладки"""
        self.operations_tab.pack_forget()
        self.ai_tab.pack_forget()
        self.history_tab.pack_forget()
        self.settings_tab.pack_forget()
        self.logs_tab.pack_forget()

    def show_operations_tab(self):
        self.hide_all_tabs()
        self.operations_tab.pack(fill="both", expand=True)

    def _open_monitor_tab(self):
        if hasattr(self, "operations_tabs"):
            self.operations_tabs.set("Мониторинг")

    def show_ai_tab(self):
        self.hide_all_tabs()
        self.ai_tab.pack(fill="both", expand=True)

    def show_history_tab(self):
        self.hide_all_tabs()
        self.history_tab.pack(fill="both", expand=True)
        self.refresh_history()

    def show_settings_tab(self):
        self.hide_all_tabs()
        self.settings_tab.pack(fill="both", expand=True)

    def show_logs_tab(self):
        self.hide_all_tabs()
        self.logs_tab.pack(fill="both", expand=True)
        self.refresh_logs()

    def check_connection(self):
        """Проверка подключения"""
        def check():
            success, message = self.jira_service.test_connection()
            def update_ui():
                if success:
                    self.connection_label.configure(text="● Подключено", text_color="green")
                else:
                    self.connection_label.configure(text="● Ошибка", text_color="red")
            self.after(0, update_ui)
        threading.Thread(target=check, daemon=True).start()

    def _release_status_color(self, status_name: str) -> str:
        status = (status_name or "").strip().lower()
        if any(token in status for token in ("done", "ready", "resolved", "closed", "готов", "закрыт")):
            return "#2E7D32"
        if any(token in status for token in ("blocked", "error", "failed", "отклон", "ошиб", "rejected")):
            return "#C62828"
        if any(token in status for token in ("progress", "review", "тест", "в работе")):
            return "#EF6C00"
        return "#1565C0"

    def refresh_release_status(self):
        release_key = self.release_entry.get().strip().upper()
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return
        issue = self.jira_service.get_issue_details(release_key)
        if not issue:
            self.release_status_value.configure(text="не найден", text_color="red")
            messagebox.showerror("Ошибка", f"Не удалось получить релиз {release_key}")
            return
        status = issue.get("fields", {}).get("status", {}).get("name", "Unknown")
        self.release_status_value.configure(text=status, text_color=self._release_status_color(status))

    def move_release_status_manual(self):
        release_key = self.release_entry.get().strip().upper()
        target_status = self.target_status_entry.get().strip()
        if not release_key or not target_status:
            messagebox.showwarning("Ошибка", "Заполните ключ релиза и целевой статус")
            return

        ok, msg = self.jira_service.transition_issue(release_key, target_status)
        if ok:
            self.refresh_release_status()
            self.add_result(f"✅ {msg}")
            self.history.add("Смена статуса релиза", {"release_key": release_key, "target_status": target_status})
            self.history.save_to_file(self.history_path)
            messagebox.showinfo("Готово", msg)
        else:
            self.add_result(f"❌ {msg}")
            messagebox.showerror("Ошибка", msg)

    def run_ai_release_pipeline(self):
        release_key = self.release_entry.get().strip().upper()
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return

        project_guess = "SFILE"
        if "-" in (self.version_entry.get().strip() or ""):
            project_guess = "SFILE"

        command = (
            f"Запусти полный пайплайн для {release_key}, "
            f"проект {project_guess}, create_bt=true, create_deploy=true"
        )
        self.show_ai_tab()
        self.send_ai_quick_command(command)

    def start_link_issues_from_ai(self, release_key: str, fix_version: str) -> str:
        """Старт привязки задач из AI-чата через существующий рабочий скрипт."""
        safe_release = (release_key or "").strip().upper()
        safe_version = (fix_version or "").strip()
        if not safe_release:
            return "Ошибка: release_key не указан."
        if not safe_version:
            return "Ошибка: fix_version не указан."

        def start_on_ui_thread():
            self.release_entry.delete(0, "end")
            self.release_entry.insert(0, safe_release)
            self.version_entry.delete(0, "end")
            self.version_entry.insert(0, safe_version)
            self.run_operation("link", self._link_issues_thread, safe_release, safe_version, True)

        self.after(0, start_on_ui_thread)
        return (
            f"Запущена привязка задач с fixVersion '{safe_version}' в релиз '{safe_release}'. "
            "Прогресс смотри во вкладке Мониторинг."
        )

    def link_issues(self):
        release_key = self.release_entry.get().strip()
        fix_version = self.version_entry.get().strip()
        if not release_key or not fix_version:
            messagebox.showwarning("Ошибка", "Заполните все поля!")
            return
        self.run_operation("link", self._link_issues_thread, release_key, fix_version, False)

    def cleanup_issues(self):
        release_key = self.release_entry.get().strip()
        fix_version = self.version_entry.get().strip()
        if not release_key or not fix_version:
            messagebox.showwarning("Ошибка", "Заполните все поля!")
            return
        self.run_operation("cleanup", self._cleanup_issues_thread, release_key, fix_version)

    def remove_all_issues(self):
        release_key = self.release_entry.get().strip()
        fix_version = self.version_entry.get().strip()
        if not release_key or not fix_version:
            messagebox.showwarning("Ошибка", "Заполните все поля!")
            return
        if not messagebox.askyesno("Подтверждение", f"Удалить ВСЕ связи для {release_key}?\\n\\nЭто действие необратимо!"):
            return
        self.run_operation("remove_all", self._remove_all_issues_thread, release_key, fix_version)

    def run_lt_check(self):
        release_key = self.release_entry.get().strip()
        try:
            target_lt = float(self.target_lt_entry.get().strip())
        except ValueError:
            messagebox.showwarning("Ошибка", "Целевой LT должен быть числом!")
            return
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return

        self._open_monitor_tab()
        self.results_text.delete("1.0", "end")
        self.results_text.insert("1.0", f"Запуск проверки LT для {release_key}...\\n")

        def process():
            try:
                report = run_lt_check_with_target(release_key, target_lt)
                def show_report():
                    self.results_text.delete("1.0", "end")
                    self.results_text.insert("1.0", report)
                self.after(0, show_report)
                self.history.add("Проверка LT", {'release': release_key, 'target_lt': target_lt})
                self.history.save_to_file(self.history_path)
            except Exception as e:
                error_msg = str(e)
                def show_error():
                    self.results_text.delete("1.0", "end")
                    self.results_text.insert("1.0", f"Ошибка: {error_msg}")
                self.after(0, show_error)

        threading.Thread(target=process, daemon=True).start()

    def run_rqg_check(self):
        release_key = self.release_entry.get().strip()
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return

        self._open_monitor_tab()
        self.results_text.delete("1.0", "end")
        self.results_text.insert("1.0", f"Запуск RQG-проверки для {release_key}...\\n")

        def process():
            try:
                report = run_rqg_check(
                    self.jira_service,
                    release_key,
                    max_depth=2,
                    trigger_button=True,
                )

                def show_report():
                    self.results_text.delete("1.0", "end")
                    self.results_text.insert("1.0", report)

                self.after(0, show_report)
                self.history.add("RQG-проверка", {'release': release_key})
                self.history.save_to_file(self.history_path)
            except Exception as e:
                error_msg = str(e)

                def show_error():
                    self.results_text.delete("1.0", "end")
                    self.results_text.insert("1.0", f"Ошибка RQG-проверки: {error_msg}")

                self.after(0, show_error)

        threading.Thread(target=process, daemon=True).start()

    def run_release_pr_status_ui(self):
        release_key = self.release_entry.get().strip().upper()
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return
        self.start_release_pr_status_check(release_key=release_key, announce_in_chat=False)

    def start_release_pr_status_check(self, release_key: str, announce_in_chat: bool = False) -> str:
        """Запуск проверки Story/Bug + PR статусов в отдельном потоке."""
        safe_release = (release_key or "").strip().upper()
        if not safe_release:
            return "Ошибка: ключ релиза не указан."

        self.after(0, self._open_monitor_tab)
        self.after(0, lambda: self.results_text.delete("1.0", "end"))
        self.after(0, lambda: self.progress_bar.set(0))
        self.after(0, lambda: self.update_status(f"Проверка задач и PR для {safe_release}..."))
        self.after(0, lambda: self.details_label.configure(text="Подготовка данных..."))

        worker = threading.Thread(
            target=self._release_pr_status_thread,
            args=(safe_release, announce_in_chat),
            daemon=True,
        )
        worker.start()
        return f"Проверка задач и PR для {safe_release} запущена. Прогресс отображается во вкладке Мониторинг."

    def _release_pr_status_thread(self, release_key: str, announce_in_chat: bool):
        try:
            def progress_callback(progress: float, detail: str):
                self.after(0, lambda p=progress, d=detail: self.update_progress(p, d))

            report_data = collect_release_tasks_pr_status(
                jira_service=self.jira_service,
                release_key=release_key,
                progress_callback=progress_callback,
            )
            report_text = format_release_tasks_pr_report(report_data)

            def show_report():
                self.results_text.delete("1.0", "end")
                self.results_text.insert("1.0", report_text)
                self.update_progress(1.0, "Проверка завершена")
                self.update_status("Готово")

            self.after(0, show_report)
            self.history.add("Проверка задач и PR", {"release": release_key})
            self.history.save_to_file(self.history_path)

            if announce_in_chat:
                self.append_ai_chat(f"🤖 Blast AI: Проверка задач и PR для {release_key} завершена.\n{report_text}\n\n")
        except Exception as e:
            error_text = f"Ошибка проверки задач и PR: {e}"

            def show_error():
                self.results_text.insert("end", f"\n❌ {error_text}\n")
                self.update_status("Ошибка")

            self.after(0, show_error)
            if announce_in_chat:
                self.append_ai_chat(f"⚠️ {error_text}\n\n")

    def analyze_master_services(self):
        """Анализ сервисов влитых в master"""
        release_key = self.release_entry.get().strip()
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return

        if not self.master_analyzer:
            messagebox.showerror("Ошибка", "Master analyzer не инициализирован")
            return

        self._open_monitor_tab()
        self.results_text.delete("1.0", "end")
        self.results_text.insert("1.0", f"🔍 Анализ мастер-веток для {release_key}...\\n\\n")
        self.deploy_btn.configure(state="disabled")

        def process():
            try:
                self.after(0, lambda: self.update_status("Анализ релиза..."))
                self.current_analysis = self.master_analyzer.analyze_release(release_key)

                def show_result():
                    self.results_text.delete("1.0", "end")

                    if self.current_analysis['success']:
                        report = f"✅ Анализ завершён: {self.current_analysis['message']}\\n"
                        report += f"{'='*60}\\n\\n"
                        report += f"📊 Статистика:\\n"
                        report += f"   • Всего задач в релизе: {self.current_analysis['total_tasks']}\\n"
                        report += f"   • Найдено PR: {self.current_analysis['total_prs']}\\n"
                        report += f"   • Сервисов в master: {len(self.current_analysis['services'])}\\n\\n"

                        if self.current_analysis['services']:
                            report += f"🚀 Сервисы влитые в master ({len(self.current_analysis['services'])}):\\n"
                            report += f"{'-'*60}\\n"
                            for i, service in enumerate(self.current_analysis['services'], 1):
                                report += f"{i:2d}. {service}\\n"

                            report += f"\\n📋 Детали по задачам:\\n"
                            report += f"{'-'*60}\\n"

                            for detail in self.current_analysis['pr_details'][:20]:
                                if detail['status'] == 'merged_to_master':
                                    report += f"✅ {detail['issue']:<15} → {detail['service']}\\n"

                            self.deploy_btn.configure(state="normal")
                        else:
                            report += "⚠️ Сервисов в master не найдено\\n"

                        self.results_text.insert("1.0", report)
                        self.update_status("Готово")
                    else:
                        error_report = f"❌ Ошибка: {self.current_analysis['message']}\\n"
                        self.results_text.insert("1.0", error_report)
                        self.update_status("Ошибка")

                    self.history.add("Анализ Master", {
                        'release': release_key,
                        'services_count': len(self.current_analysis.get('services', [])),
                        'success': self.current_analysis['success']
                    })
                    self.history.save_to_file(self.history_path)

                self.after(0, show_result)

            except Exception as e:
                error_msg = str(e)
                self.logger.error(f"Ошибка анализа master: {error_msg}", exc_info=True)

                def show_error():
                    self.results_text.delete("1.0", "end")
                    self.results_text.insert("1.0", f"❌ Критическая ошибка:\\n\\n{error_msg}")
                    self.update_status("Ошибка")

                self.after(0, show_error)

        threading.Thread(target=process, daemon=True).start()

    def create_deploy_plan(self):
        """Создание деплой-плана в Confluence"""
        if not self.current_analysis or not self.current_analysis.get('success'):
            messagebox.showerror("Ошибка", "Сначала выполните анализ мастер-веток")
            return

        services = self.current_analysis.get('services', [])
        if not services:
            messagebox.showwarning("Предупреждение", "Нет сервисов для деплоя")
            return

        if not self.confluence_generator:
            messagebox.showerror("Ошибка", "Confluence не настроен. Проверьте .env")
            return

        # Подтверждение
        services_preview = ', '.join(services[:5])
        if len(services) > 5:
            services_preview += f"... (и еще {len(services) - 5})"

        confirm = messagebox.askyesno(
            "Подтверждение",
            f"Создать деплой-план для {len(services)} сервисов?\\n\\n"
            f"Релиз: {self.current_analysis['release_key']}\\n"
            f"Название: {self.current_analysis.get('release_summary', 'N/A')}\\n"
            f"Сервисы: {services_preview}\\n\\n"
            f"Страница будет создана в Confluence:\\n"
            f"Space: {CONFLUENCE_SPACE_KEY}\\n"
            f"Родитель: {CONFLUENCE_PARENT_PAGE_TITLE}\\n"
            f"Команда: {TEAM_NAME}"
        )

        if not confirm:
            return

        self.deploy_btn.configure(state="disabled")
        self.update_status("📝 Создание деплой-плана...")

        def process():
            try:
                result = self.master_analyzer.generate_deploy_plan(
                    analysis_result=self.current_analysis,
                    space_key=CONFLUENCE_SPACE_KEY,
                    parent_page_title=CONFLUENCE_PARENT_PAGE_TITLE,
                    team_name=TEAM_NAME
                )

                def update_ui():
                    if result['success']:
                        page_url = result['page_url']
                        page_title = result['page_title']

                        self.update_status("✅ Деплой-план создан")

                        msg = f"✅ Деплой-план успешно создан!\\n\\n"
                        msg += f"Название: {page_title}\\n"
                        msg += f"Сервисов: {len(services)}\\n\\n"
                        msg += f"Открыть страницу в браузере?"

                        open_page = messagebox.askyesno("Успех", msg)

                        if open_page:
                            webbrowser.open(page_url)

                        self.results_text.insert("end", f"\\n{'='*60}\\n")
                        self.results_text.insert("end", f"✅ ДЕПЛОЙ-ПЛАН СОЗДАН\\n")
                        self.results_text.insert("end", f"{'='*60}\\n")
                        self.results_text.insert("end", f"📄 {page_title}\\n")
                        self.results_text.insert("end", f"🔗 {page_url}\\n")

                        self.history.add("Создание деплой-плана", {
                            'release': self.current_analysis['release_key'],
                            'services_count': len(services),
                            'page_url': page_url
                        })
                        self.history.save_to_file(self.history_path)

                    else:
                        error_msg = result.get('message', 'Неизвестная ошибка')
                        details = result.get('details', '')
                        self.update_status("❌ Ошибка создания")
                        messagebox.showerror("Ошибка", f"Не удалось создать деплой-план:\\n\\n{error_msg}\\n\\n{details}")

                    self.deploy_btn.configure(state="normal")

                self.after(0, update_ui)

            except Exception as e:
                error_msg = str(e)
                self.logger.error(f"Ошибка создания деплой-плана: {error_msg}", exc_info=True)
                self.after(0, lambda: messagebox.showerror("Ошибка", f"Ошибка:\\n{error_msg}"))
                self.after(0, lambda: self.deploy_btn.configure(state="normal"))
                self.after(0, lambda: self.update_status("❌ Ошибка"))

        threading.Thread(target=process, daemon=True).start()

    def run_operation(self, operation_type, target_func, *args):
        self.cancel_operation = False
        self.current_operation = operation_type
        self._open_monitor_tab()
        self.set_ui_enabled(False)
        self.cancel_btn.configure(state="normal")
        self.results_text.delete("1.0", "end")
        self.progress_bar.set(0)
        thread = threading.Thread(target=target_func, args=args, daemon=True)
        thread.start()

    def _link_issues_thread(self, release_key, fix_version, announce_in_chat: bool = False):
        """Поток привязки задач"""
        try:
            self.after(0, lambda: self.update_status("Поиск задач..."))
            jql = f'project IN (HRM, HRC, NEUROUI, SFILE, SEARCHCS) AND issuetype IN (Bug, Story) AND fixVersion = "{fix_version}"'
            issues = self.jira_service.search_issues(jql)

            if not issues:
                self.after(0, lambda: self.show_result("Информация", "Нет задач для привязки"))
                return

            link_types = self.jira_service.get_link_types()
            link_type_name = next((name for name in link_types if 'part' in name.lower()), None)

            if not link_type_name:
                self.after(0, lambda: self.show_result("Ошибка", "Не найден подходящий тип связи"))
                return

            self.after(0, lambda: self.update_status("Проверка существующих связей..."))
            already_linked = set(self.jira_service.get_linked_issues(release_key))

            issues_to_link = [issue for issue in issues if issue['key'] not in already_linked and issue['key'] != release_key]

            if not issues_to_link:
                msg = f"Все задачи уже привязаны к {release_key}"
                self.after(0, lambda m=msg: self.show_result("Информация", m))
                return

            total = len(issues_to_link)
            success_count = 0
            errors = []

            for i, issue in enumerate(issues_to_link, 1):
                if self.cancel_operation:
                    break
                issue_key = issue['key']
                if self.dry_run_var.get():
                    msg = f"🔍 {issue_key} (тестовый режим)"
                    self.after(0, lambda m=msg: self.add_result(m))
                    success_count += 1
                else:
                    if self.jira_service.create_issue_link(issue_key, release_key, link_type_name):
                        success_count += 1
                        msg = f"✅ {issue_key}"
                        self.after(0, lambda m=msg: self.add_result(m))
                    else:
                        errors.append(issue_key)
                        msg = f"❌ {issue_key}"
                        self.after(0, lambda m=msg: self.add_result(m))
                progress = i / total
                text = f"Обработано: {i}/{total}"
                self.after(0, lambda p=progress, t=text: self.update_progress(p, t))

            self.history.add("Привязка задач", {
                "release_key": release_key,
                "fix_version": fix_version,
                "total": total,
                "success": success_count,
                "errors": len(errors)
            })
            self.history.save_to_file(self.history_path)

            message = f"Успешно: {success_count}/{total}"
            if errors:
                message += f"\\nОшибки: {len(errors)}"
            self.after(0, lambda m=message: self.show_result("Готово", m))
            if announce_in_chat:
                self.append_ai_chat(
                    f"🤖 Blast AI: Привязка задач в релиз {release_key} завершена.\n{message}\n\n"
                )

        except Exception as e:
            error_msg = str(e)
            self.after(0, lambda m=error_msg: self.show_result("Ошибка", m))
            if announce_in_chat:
                self.append_ai_chat(f"⚠️ Ошибка привязки задач: {error_msg}\n\n")
        finally:
            self.after(0, lambda: self.set_ui_enabled(True))
            self.after(0, lambda: self.cancel_btn.configure(state="disabled"))

    def _cleanup_issues_thread(self, release_key, fix_version):
        """Поток очистки связей"""
        try:
            self.after(0, lambda: self.update_status("Получение связанных задач..."))
            linked_issues = self.jira_service.get_linked_issues(release_key)

            if not linked_issues:
                self.after(0, lambda: self.show_result("Информация", "Нет связанных задач"))
                return

            total = len(linked_issues)
            removed_count = 0

            for i, issue_key in enumerate(linked_issues, 1):
                if self.cancel_operation:
                    break
                progress = i / total
                text = f"Проверка: {i}/{total}"
                self.after(0, lambda p=progress, t=text: self.update_progress(p, t))

                issue_data = self.jira_service.get_issue_details(issue_key)
                if not issue_data:
                    continue

                fields = issue_data.get('fields', {})
                version_names = [v.get('name') for v in fields.get('fixVersions', [])]

                if fix_version not in version_names:
                    for link in fields.get('issuelinks', []):
                        if (
                            ('outwardIssue' in link and link['outwardIssue']['key'] == release_key)
                            or ('inwardIssue' in link and link['inwardIssue']['key'] == release_key)
                        ):
                            link_id = link['id']
                            if self.jira_service.delete_issue_link(link_id):
                                removed_count += 1
                                msg = f"✅ {issue_key}"
                                self.after(0, lambda m=msg: self.add_result(m))
                            break

            self.history.add("Очистка связей", {
                "release_key": release_key,
                "fix_version": fix_version,
                "total": total,
                "removed": removed_count
            })
            self.history.save_to_file(self.history_path)

            message = f"Удалено: {removed_count}/{total}"
            self.after(0, lambda m=message: self.show_result("Готово", m))

        except Exception as e:
            error_msg = str(e)
            self.after(0, lambda m=error_msg: self.show_result("Ошибка", m))
        finally:
            self.after(0, lambda: self.set_ui_enabled(True))
            self.after(0, lambda: self.cancel_btn.configure(state="disabled"))

    def _remove_all_issues_thread(self, release_key: str, fix_version: str):
        """Поток удаления всех связей"""
        try:
            self.after(0, lambda: self.update_status("Получение связанных задач..."))
            linked_issues = self.jira_service.get_linked_issues(release_key)

            if not linked_issues:
                self.after(0, lambda: self.show_result("Информация", "Нет связанных задач"))
                return

            total = len(linked_issues)
            removed_count = 0
            errors = []

            for i, issue_key in enumerate(linked_issues, 1):
                if self.cancel_operation:
                    break
                progress = i / total
                text = f"Удаление: {i}/{total}"
                self.after(0, lambda p=progress, t=text: self.update_progress(p, t))

                issue_data = self.jira_service.get_issue_details(issue_key)
                if not issue_data:
                    continue

                fields = issue_data.get('fields', {})
                version_names = [v.get('name') for v in fields.get('fixVersions', [])]

                if fix_version in version_names:
                    for link in fields.get('issuelinks', []):
                        if (
                            ('outwardIssue' in link and link['outwardIssue']['key'] == release_key)
                            or ('inwardIssue' in link and link['inwardIssue']['key'] == release_key)
                        ):
                            if self.dry_run_var.get():
                                msg = f"🔍 {issue_key} (тестовый режим)"
                                self.after(0, lambda m=msg: self.add_result(m))
                                removed_count += 1
                            else:
                                link_id = link['id']
                                if self.jira_service.delete_issue_link(link_id):
                                    removed_count += 1
                                    msg = f"✅ {issue_key} удалена связь"
                                    self.after(0, lambda m=msg: self.add_result(m))
                                else:
                                    errors.append(issue_key)
                                    msg = f"❌ {issue_key} ошибка"
                                    self.after(0, lambda m=msg: self.add_result(m))
                            break

            self.history.add("Удаление всех связей", {
                "release_key": release_key,
                "fix_version": fix_version,
                "total": total,
                "removed": removed_count,
                "errors": len(errors)
            })
            self.history.save_to_file(self.history_path)

            message = f"Удалено: {removed_count}/{total}"
            if errors:
                message += f"\\nОшибки: {len(errors)}"
            self.after(0, lambda m=message: self.show_result("Готово", m))

        except Exception as e:
            error_msg = str(e)
            self.after(0, lambda m=error_msg: self.show_result("Ошибка", m))
        finally:
            self.after(0, lambda: self.set_ui_enabled(True))
            self.after(0, lambda: self.cancel_btn.configure(state="disabled"))


    def cancel_current_operation(self):
        self.cancel_operation = True
        self.after(0, lambda: self.update_status("Отмена операции..."))

    def save_settings(self):
        self.config.url = self.url_entry.get()
        self.config.token = self.token_entry.get()
        self.config.verify_ssl = self.ssl_var.get()
        self.config.save_to_file(self.config_path)
        self.jira_service = JiraService(self.config)
        messagebox.showinfo("Успех", "Настройки сохранены")
        self.check_connection()

    def test_connection(self):
        success, message = self.jira_service.test_connection()
        if success:
            messagebox.showinfo("Успех", message)
        else:
            messagebox.showerror("Ошибка", message)

    def refresh_history(self):
        self.history_text.delete("1.0", "end")
        for entry in self.history.get_recent(50):
            timestamp = datetime.fromisoformat(entry['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            operation = entry['operation']
            details = entry['details']
            line = f"[{timestamp}] {operation} - {details}\\n"
            self.history_text.insert("end", line)

    def clear_history(self):
        if messagebox.askyesno("Подтверждение", "Очистить всю историю?"):
            self.history.history.clear()
            self.history.save_to_file(self.history_path)
            self.refresh_history()

    def refresh_logs(self):
        try:
            log_file = os.path.join(self.config_dir, 'app.log')
            if os.path.exists(log_file):
                with open(log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()[-1000:]
                    self.log_text.delete("1.0", "end")
                    for line in lines:
                        tag = "log_info"
                        if " - ERROR - " in line:
                            tag = "log_error"
                        elif " - WARNING - " in line:
                            tag = "log_warning"
                        self.log_text.insert("end", line, tag)
        except Exception as e:
            self.log_text.insert("1.0", f"Ошибка чтения логов: {str(e)}")

    def clear_logs(self):
        if messagebox.askyesno("Подтверждение", "Очистить логи?"):
            self.log_text.delete("1.0", "end")
            log_file = os.path.join(self.config_dir, 'app.log')
            if os.path.exists(log_file):
                os.remove(log_file)

    def update_status(self, message):
        text = str(message or "")
        color = "#1565C0"
        lowered = text.lower()
        if "ошиб" in lowered or "error" in lowered:
            color = "#C62828"
        elif "готов" in lowered or "успеш" in lowered or "done" in lowered:
            color = "#2E7D32"
        elif "отмена" in lowered or "warning" in lowered:
            color = "#EF6C00"
        self.progress_label.configure(text=text, text_color=color)

    def update_progress(self, value, text=""):
        self.progress_bar.set(value)
        self.details_label.configure(text=text)

    def add_result(self, text):
        message = str(text or "").strip()
        tag = "info"
        if message.startswith("✅"):
            tag = "success"
        elif message.startswith("❌"):
            tag = "error"
        elif message.startswith("⚠️"):
            tag = "warning"
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\\n"
        self.results_text.insert("end", line, tag)
        self.results_text.see("end")

    def show_result(self, title, message):
        self.update_status(title)
        if title != "Ошибка":
            messagebox.showinfo(title, message)
        else:
            messagebox.showerror(title, message)

    def set_ui_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.link_btn.configure(state=state)
        self.cleanup_btn.configure(state=state)
        self.remove_all_btn.configure(state=state)
        self.lt_btn.configure(state=state)
        self.rqg_btn.configure(state=state)
        self.pr_status_btn.configure(state=state)
        self.master_btn.configure(state=state)
        self.release_entry.configure(state=state)
        self.version_entry.configure(state=state)


if __name__ == "__main__":
    app = ModernJiraApp()
    app.mainloop()