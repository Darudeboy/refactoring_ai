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
from config import JiraConfig, CONFLUENCE_URL, CONFLUENCE_TOKEN, CONFLUENCE_SPACE_KEY, CONFLUENCE_PARENT_PAGE_TITLE, CONFLUENCE_TEMPLATE_PAGE_ID, TEAM_NAME, LINK_TASKS_PROJECTS
from service import JiraService
from history import OperationHistory
from lt import run_lt_check_with_target
from rqg import run_rqg_check
from release_pr_status import (
    collect_release_tasks_pr_status,
    format_release_tasks_pr_report,
)
from release_flow import evaluate_release_gates, format_release_gate_report
from arch import JIRA_TOKEN as ARCH_JIRA_TOKEN
from master_analyzer import MasterServicesAnalyzer, ConfluenceDeployPlanGenerator
from theme import (
    PRIMARY,
    PRIMARY_HOVER,
    NEUTRAL_BG_SIDEBAR,
    NEUTRAL_BG_CARD,
    NEUTRAL_TEXT,
    NEUTRAL_TEXT_SECONDARY,
    NEUTRAL_TEXT_MUTED,
    SEMANTIC_SUCCESS,
    SEMANTIC_SUCCESS_HOVER,
    SEMANTIC_WARNING,
    SEMANTIC_WARNING_HOVER,
    SEMANTIC_ERROR,
    SEMANTIC_ERROR_HOVER,
    SEMANTIC_INFO,
    CHAT_USER,
    CHAT_BOT,
    CHAT_TOOL,
    CHAT_ERROR,
    CHAT_DEFAULT,
    STATUS_OK,
    STATUS_ERROR,
    STATUS_PENDING,
    FONT_HEADING,
    FONT_SUBHEADING,
    FONT_BODY,
    FONT_LABEL,
    FONT_CAPTION,
    FONT_MONO,
    SPACE_SM,
    SPACE_MD,
    SPACE_LG,
    CORNER_RADIUS,
    BUTTON_HEIGHT_PRIMARY,
    BUTTON_HEIGHT_SECONDARY,
    SIDEBAR_WIDTH,
)

# === ИМПОРТЫ ДЛЯ ИИ-АГЕНТА ===
from langchain_core.messages import BaseMessage, AIMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.tools import tool
from langchain_core.runnables import Runnable
from langgraph.graph import StateGraph, END
from typing import Any, List, TypedDict, Annotated, Sequence

ctk.set_appearance_mode("light")

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
        self.pending_bt_release_key: str | None = None
        self.pending_arch_release_key: str | None = None
        self.llm = SberGigaChatHR().bind_tools([])
        self._setup_graph()

    def _extract_function_style_calls(self, content: str):
        """Парсит вызовы вида tool_name("arg1", key="value") из ответа модели."""
        if not content:
            return [], ""

        arg_names_map = {
            "get_jira_status": ["issue_key"],
            "create_deploy_plan": ["issue_key"],
            "create_business_requirements": ["issue_key", "project_key"],
            "check_lead_time": ["release_key"],
            "check_rqg": ["release_key", "max_depth", "trigger_button"],
            "update_architecture_status": ["release_key"],
            "move_release_status": ["issue_key", "target_status"],
            "run_release_pipeline": ["issue_key", "project_key", "target_lt", "create_bt", "create_deploy"],
            "check_release_tasks_pr_status": ["release_key"],
            "link_tasks_to_release": ["release_key", "fix_version"],
            "link_issues_by_fix_version": ["release_key", "fix_version"],
            "start_release_guided_cycle": ["release_key", "profile", "dry_run"],
            "run_next_release_step": ["release_key", "dry_run"],
            "confirm_manual_check": ["release_key", "check_id", "result"],
            "move_release_if_ready": ["release_key", "dry_run"],
        }

        tool_calls = []
        remaining = content
        for match in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_]*)\(([^()]*)\)", content, re.DOTALL):
            full_expr = match.group(0)
            func_name = match.group(1)
            if func_name not in arg_names_map:
                continue

            try:
                expr = ast.parse(full_expr, mode="eval")
            except Exception:
                continue

            if not isinstance(expr, ast.Expression) or not isinstance(expr.body, ast.Call):
                continue

            call_node = expr.body
            args_dict = {}

            positional_names = arg_names_map.get(func_name, [])
            for idx, arg_node in enumerate(call_node.args):
                if idx >= len(positional_names):
                    break
                try:
                    args_dict[positional_names[idx]] = ast.literal_eval(arg_node)
                except Exception:
                    args_dict[positional_names[idx]] = None

            for kw in call_node.keywords:
                if not kw.arg:
                    continue
                try:
                    args_dict[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    args_dict[kw.arg] = None

            tool_calls.append(
                {"name": func_name, "args": args_dict, "id": os.urandom(8).hex()}
            )
            remaining = remaining.replace(full_expr, "")

        return tool_calls, remaining.strip()

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

            name_key = None
            for k in ("tool", "function", "name"):
                if parsed.get(k) and isinstance(parsed[k], str):
                    name_key = k
                    break

            if name_key and name_key in ("tool", "function"):
                args = {k: v for k, v in parsed.items() if k != name_key}
                add_tool_call(parsed[name_key], args)
                remaining = remaining.replace(candidate, "").strip()
            elif name_key == "name" and isinstance(parsed.get("args"), dict):
                add_tool_call(parsed["name"], parsed["args"])
                remaining = remaining.replace(candidate, "").strip()
            elif name_key == "name" and isinstance(parsed.get("arguments"), dict):
                add_tool_call(parsed["name"], parsed["arguments"])
                remaining = remaining.replace(candidate, "").strip()
            elif name_key == "name":
                args = {k: v for k, v in parsed.items() if k != "name"}
                add_tool_call(parsed["name"], args)
                remaining = remaining.replace(candidate, "").strip()
            elif isinstance(parsed.get("commands"), list):
                for cmd in parsed["commands"]:
                    if not isinstance(cmd, dict):
                        continue
                    n = cmd.get("tool") or cmd.get("function") or cmd.get("name")
                    args = cmd.get("args", cmd.get("arguments", {}))
                    if n:
                        add_tool_call(n, args)
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

                ak = None
                for k in ("tool", "function", "name"):
                    if action.get(k) and isinstance(action[k], str):
                        ak = k
                        break

                if ak:
                    args = {k: v for k, v in action.items() if k != ak}
                    add_tool_call(action[ak], args)
                    remaining = remaining.replace(match, "")

        if not tool_calls:
            function_calls, function_remaining = self._extract_function_style_calls(remaining)
            if function_calls:
                tool_calls.extend(function_calls)
                remaining = function_remaining

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
            Выполнить RQG-проверку релиза: статусы задач, наличие БТ, дистрибутивы.

            Args:
                release_key: Ключ релиза Jira (например, HRPRELEASE-111135).
                max_depth: Глубина обхода связанных задач (обычно 2).
                trigger_button: Попробовать нажать системную кнопку RQG в Jira (True по умолчанию).
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
            Проставляет поле архитектуры для Story в составе релиза.
            Используй, если просят: "проставь архитектуру для сторей в релизе",
            "проставь архитектуру", "закрой архитектурные задачи", "арх не меняется".
            """
            release_key = (release_key or "").strip().upper()
            if not release_key:
                return "Ошибка: не передан release_key (пример: HRPRELEASE-111135)."
            if not re.fullmatch(r"HRPRELEASE-\d+", release_key, re.IGNORECASE):
                return (
                    "Ошибка: ожидается ключ релиза вида HRPRELEASE-123456. "
                    f"Получено: {release_key}"
                )
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Проставляю архитектуру для Story в релизе {release_key}...\\n"
            )
            return self.app_gui.start_architecture_update_from_ai(
                release_key=release_key,
                announce_in_chat=True,
            )

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

        @tool("start_release_guided_cycle")
        def start_release_guided_cycle(release_key: str, profile: str = "auto", dry_run: bool = False) -> str:
            """
            Запускает пошаговый Guided Cycle для полного релизного процесса.

            Args:
                release_key: Ключ релиза Jira (например, HRPRELEASE-113937).
                profile: Профиль правил ('auto', 'default', 'hotfix' или кастомный).
                dry_run: Если True, только оценка/отчет без реального перевода статусов.
            """
            release_key = (release_key or "").strip().upper()
            profile = (profile or "auto").strip().lower()
            if not release_key:
                return "Ошибка: не передан release_key (пример: HRPRELEASE-113937)."
            return self.app_gui.start_release_guided_cycle(
                release_key=release_key,
                profile=profile,
                dry_run=dry_run,
                announce_in_chat=True,
            )

        @tool("run_next_release_step")
        def run_next_release_step(release_key: str, dry_run: bool = False) -> str:
            """
            Выполняет следующий шаг guided-цикла для релиза:
            переоценивает гейты и возвращает, что блокирует следующий переход.
            """
            release_key = (release_key or "").strip().upper()
            if not release_key:
                return "Ошибка: не передан release_key."
            return self.app_gui.run_next_release_step(
                release_key=release_key,
                dry_run=dry_run,
                announce_in_chat=True,
            )

        @tool("confirm_manual_check")
        def confirm_manual_check(release_key: str, check_id: str, result: str) -> str:
            """
            Подтверждает ручную проверку guided-цикла.

            Args:
                release_key: Ключ релиза.
                check_id: Идентификатор проверки (например, decommission_distribution).
                result: Результат ('ok'/'fail').
            """
            release_key = (release_key or "").strip().upper()
            check_id = (check_id or "").strip()
            result = (result or "").strip().lower()
            if not release_key or not check_id:
                return "Ошибка: укажи release_key и check_id."
            if result not in ("ok", "fail", "true", "false", "yes", "no", "да", "нет"):
                return "Ошибка: result должен быть ok/fail."
            return self.app_gui.confirm_manual_check(
                release_key=release_key,
                check_id=check_id,
                result=result,
                announce_in_chat=True,
            )

        @tool("move_release_if_ready")
        def move_release_if_ready(release_key: str, dry_run: bool = False) -> str:
            """
            Переводит релиз в следующий статус workflow, если все auto/manual гейты пройдены.
            """
            release_key = (release_key or "").strip().upper()
            if not release_key:
                return "Ошибка: не передан release_key."
            return self.app_gui.move_release_if_ready(
                release_key=release_key,
                dry_run=dry_run,
                announce_in_chat=True,
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
            "start_release_guided_cycle": start_release_guided_cycle,
            "run_next_release_step": run_next_release_step,
            "confirm_manual_check": confirm_manual_check,
            "move_release_if_ready": move_release_if_ready,
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
                "Твоя задача — по фразе пользователя вызвать нужный инструмент и вернуть понятный итог.\\n\\n"
                "ФОРМАТ КЛЮЧЕЙ: release_key всегда вида HRPRELEASE-123456; fix_version — например HM-REL-05-03-2026 или WEB-2026.03.X.\\n\\n"
                "ДОСТУПНЫЕ ИНСТРУМЕНТЫ:\\n"
                "1) get_jira_status(issue_key) — статус задачи/релиза.\\n"
                "2) create_deploy_plan(issue_key) — создать deploy plan в Confluence.\\n"
                "3) create_business_requirements(issue_key, project_key) — создать БТ/ФР; project_key обязателен (SFILE, HRC, HRM и т.д.).\\n"
                "4) check_lead_time(release_key) — лид-тайм по релизу.\\n"
                "5) check_rqg(release_key, max_depth=2) — RQG-проверка.\\n"
                "6) update_architecture_status(release_key) — проставить архитектуру по сторам релиза.\\n"
                "7) move_release_status(issue_key, target_status) — перевести релиз в указанный статус (например ПСИ, Ready for Prod).\\n"
                "8) run_release_pipeline(issue_key, project_key='', create_bt=False, create_deploy=False) — полный пайплайн.\\n"
                "9) check_release_tasks_pr_status(release_key) — задачи релиза и статусы PR.\\n"
                "10) link_tasks_to_release(release_key, fix_version) — привязать к релизу задачи с указанным fixVersion.\\n"
                "11) start_release_guided_cycle(release_key, profile='auto', dry_run=False) — полный пошаговый цикл (гейты, следующий шаг).\\n"
                "12) run_next_release_step(release_key, dry_run=False) — оценить и при готовности перевести релиз на следующий шаг.\\n"
                "13) confirm_manual_check(release_key, check_id, result) — подтвердить ручную проверку (result: ok или fail).\\n"
                "14) move_release_if_ready(release_key, dry_run=False) — сдвинуть в следующий статус только если все гейты пройдены.\\n\\n"
                "ПРИМЕРЫ КОМАНД ПРО РЕЛИЗ (фраза пользователя → инструмент):\\n"
                "- «Проверь статус HRPRELEASE-123» → get_jira_status(issue_key=\"HRPRELEASE-123\").\\n"
                "- «Привяжи задачи HM-REL-05-03-2026 в HRPRELEASE-123» / «собери задачи с версией X в релиз Y» → link_tasks_to_release(release_key=\"HRPRELEASE-123\", fix_version=\"HM-REL-05-03-2026\").\\n"
                "- «Запусти полный цикл релиза для HRPRELEASE-123» / «пошаговый цикл» → start_release_guided_cycle(release_key=\"HRPRELEASE-123\").\\n"
                "- «Двигай HRPRELEASE-123 дальше» / «следующий шаг» / «продолжи релиз» → run_next_release_step(release_key=\"HRPRELEASE-123\").\\n"
                "- «Переведи HRPRELEASE-123 в ПСИ» / «в Ready for Prod» → move_release_status(issue_key=\"HRPRELEASE-123\", target_status=\"ПСИ\").\\n"
                "- «Проверь задачи и PR для HRPRELEASE-123» → check_release_tasks_pr_status(release_key=\"HRPRELEASE-123\").\\n"
                "- «Проверь RQG для HRPRELEASE-123» → check_rqg(release_key=\"HRPRELEASE-123\").\\n"
                "- «Сделай БТ для HRPRELEASE-123, проект SFILE» → create_business_requirements(issue_key=\"HRPRELEASE-123\", project_key=\"SFILE\").\\n"
                "- «Проставь архитектуру для сторей в релизе HRPRELEASE-123» → update_architecture_status(release_key=\"HRPRELEASE-123\").\\n"
                "- «Запусти полный пайплайн для HRPRELEASE-123, проект SFILE» → run_release_pipeline(issue_key=\"HRPRELEASE-123\", project_key=\"SFILE\").\\n\\n"
                "ПРАВИЛА:\\n"
                "- Сначала вызови инструмент, не выдумывай результат. При нехватке аргументов (release_key, fix_version, project_key, target_status) задай один короткий уточняющий вопрос.\\n"
                "- Для проверки без изменений в guided-инструментах используй dry_run=True.\\n"
                "- Ответ после вызова: кратко, что сделано; итог или ошибка; что делать дальше при необходимости.\\n"
                "- Не группируй вызовы в массив; каждый вызов — отдельный JSON."
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

        def _extract_fix_version_from_text(payload: str) -> str:
            explicit = re.search(
                r"(?:верси\w*|fix\s*version|fixversion)\s*[:=]?\s*([A-Z0-9._\-]+)",
                payload,
                re.IGNORECASE,
            )
            if explicit:
                candidate = explicit.group(1).strip()
                if re.fullmatch(r"HRPRELEASE-\d+", candidate, re.IGNORECASE):
                    return ""
                if not re.fullmatch(r"[A-Z]+-\d+", candidate, re.IGNORECASE):
                    return candidate
                return ""

            labeled = re.search(
                r"(?:project|проект)\s*[:=]?\s*[A-Z0-9_]+\s*[,;]?\s*(?:fix\s*version|fixversion|верси\w*)\s*[:=]?\s*([A-Z0-9._\-]+)",
                payload,
                re.IGNORECASE,
            )
            if labeled:
                candidate = labeled.group(1).strip()
                if not re.fullmatch(r"[A-Z]+-\d+", candidate, re.IGNORECASE) and not re.fullmatch(
                    r"HRPRELEASE-\d+",
                    candidate,
                    re.IGNORECASE,
                ):
                    return candidate

            # Поддержка короткого формата "HRC HM-REL-05-03-2026" без ключевых слов.
            token_candidates = re.findall(r"\b([A-Z0-9][A-Z0-9._\-]{4,})\b", payload, re.IGNORECASE)
            for token in token_candidates:
                upper = token.upper()
                if re.fullmatch(r"[A-Z]+-\d+", upper):
                    continue
                if re.fullmatch(r"HRPRELEASE-\d+", upper):
                    continue
                if upper in {"HRC", "HRM", "NEUROUI", "SFILE", "SEARCHCS", "NEURO", "HRPDEV"}:
                    continue
                return token.strip()
            return ""

        if self.pending_bt_release_key:
            project_match = re.fullmatch(r"\s*([A-Z][A-Z0-9_]{1,15})\s*", raw, re.IGNORECASE)
            if project_match:
                project_key = project_match.group(1).upper()
                release_key = self.pending_bt_release_key
                self.pending_bt_release_key = None
                self.app_gui.append_ai_chat(
                    f"🛠️ [Агент] Получил project_key '{project_key}', запускаю bt3.py для {release_key}...\n"
                )
                result = self.app_gui.start_business_requirements_from_ai(
                    release_key=release_key,
                    project_key=project_key,
                )
                self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
                return True

        if self.pending_arch_release_key:
            project_match = re.search(
                r"\b(HRC|HRM|NEUROUI|SFILE|SEARCHCS|NEURO|HRPDEV)\b",
                raw,
                re.IGNORECASE,
            )
            fix_version = _extract_fix_version_from_text(raw)
            if project_match or fix_version:
                project_key = project_match.group(1).upper() if project_match else ""
                release_key = self.pending_arch_release_key
                self.pending_arch_release_key = None
                self.app_gui.append_ai_chat(
                    f"🛠️ [Агент] Получил уточнение для {release_key}: "
                    f"project_key='{project_key or '-'}', fix_version='{fix_version or '-'}'. "
                    "Запускаю проставление архитектуры...\n"
                )
                result = self.app_gui.start_architecture_update_from_ai(
                    release_key=release_key,
                    announce_in_chat=True,
                    forced_project_key=project_key,
                    forced_fix_version=fix_version,
                )
                self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
                return True

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
        fix_version_fallback = _extract_fix_version_from_text(raw)
        if link_intent and release_match and (version_match or fix_version_fallback):
            release_key = release_match.group(1).upper()
            fix_version = (version_match.group(1) if version_match else fix_version_fallback).strip()
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Прямая команда: привязка задач {fix_version} -> {release_key}\n"
            )
            result = self.app_gui.start_link_issues_from_ai(
                release_key=release_key,
                fix_version=fix_version,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        pr_intent = any(
            phrase in lowered
            for phrase in (
                "проверь задачи и pr",
                "задачи и pr",
                "статус pr",
                "pr статус",
                "check pr",
                "check tasks and pr",
            )
        )
        if pr_intent and release_match:
            release_key = release_match.group(1).upper()
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Прямая команда: проверка задач и PR для {release_key}\n"
            )
            result = self.app_gui.start_release_pr_status_check(
                release_key=release_key,
                announce_in_chat=True,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        pipeline_intent = any(
            phrase in lowered
            for phrase in (
                "полный пайплайн",
                "запусти пайплайн",
                "run pipeline",
                "release pipeline",
            )
        )
        if pipeline_intent and release_match:
            issue_key = release_match.group(1).upper()
            project_match = re.search(
                r"(?:project|проект)\s*[:=]?\s*(HRC|HRM|NEUROUI|SFILE|SEARCHCS|NEURO|HRPDEV)\b",
                raw,
                re.IGNORECASE,
            )
            project_key = project_match.group(1).upper() if project_match else ""

            bt_match = re.search(r"create_bt\s*[:=]\s*(true|false|1|0|yes|no|да|нет)", raw, re.IGNORECASE)
            deploy_match = re.search(
                r"create_deploy\s*[:=]\s*(true|false|1|0|yes|no|да|нет)",
                raw,
                re.IGNORECASE,
            )

            create_bt = bt_match.group(1).lower() if bt_match else "false"
            create_deploy = deploy_match.group(1).lower() if deploy_match else "false"

            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Прямая команда: запуск полного пайплайна для {issue_key} "
                f"(project={project_key or '-'}, create_bt={create_bt}, create_deploy={create_deploy})\n"
            )
            result = self.tools_map["run_release_pipeline"].invoke(
                {
                    "issue_key": issue_key,
                    "project_key": project_key,
                    "create_bt": create_bt,
                    "create_deploy": create_deploy,
                }
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        rqg_intent = any(
            phrase in lowered
            for phrase in (
                "проверь rqg",
                "rqg провер",
                "запусти rqg",
                "run rqg",
            )
        )
        if rqg_intent and release_match:
            release_key = release_match.group(1).upper()
            depth_match = re.search(r"(?:max_depth|глубин\w*)\s*[:=]?\s*(\d+)", raw, re.IGNORECASE)
            max_depth = int(depth_match.group(1)) if depth_match else 2
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Прямая команда: RQG-проверка для {release_key} (max_depth={max_depth})\n"
            )
            result = self.tools_map["check_rqg"].invoke(
                {"release_key": release_key, "max_depth": max_depth, "trigger_button": True}
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        guided_intent = any(
            phrase in lowered
            for phrase in (
                "guided cycle",
                "полный цикл релиза",
                "запусти цикл релиза",
                "пошаговый релиз",
            )
        )
        if guided_intent and release_match:
            release_key = release_match.group(1).upper()
            profile = "hotfix" if "hotfix" in lowered else "auto"
            dry_run = "dry-run" in lowered or "dry run" in lowered or "тестовый прогон" in lowered
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Прямая команда: запуск guided cycle для {release_key} "
                f"(profile={profile}, dry_run={dry_run})\n"
            )
            result = self.app_gui.start_release_guided_cycle(
                release_key=release_key,
                profile=profile,
                dry_run=dry_run,
                announce_in_chat=True,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        next_step_intent = any(
            phrase in lowered
            for phrase in ("следующий шаг", "next step", "продолжи релиз", "продолжай цикл")
        )
        if next_step_intent and release_match:
            release_key = release_match.group(1).upper()
            dry_run = "dry-run" in lowered or "dry run" in lowered or "тестовый прогон" in lowered
            result = self.app_gui.run_next_release_step(
                release_key=release_key,
                dry_run=dry_run,
                announce_in_chat=True,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        architecture_intent = any(
            phrase in lowered
            for phrase in (
                "проставь архитектуру",
                "архитектуру для сторей",
                "архитектура для сторей",
                "закрой архитектурные задачи",
                "architecture for stories",
            )
        )
        if architecture_intent and release_match:
            release_key = release_match.group(1).upper()
            project_match = re.search(
                r"\b(HRC|HRM|NEUROUI|SFILE|SEARCHCS|NEURO|HRPDEV)\b",
                raw,
                re.IGNORECASE,
            )
            forced_project = project_match.group(1).upper() if project_match else ""
            forced_fix = _extract_fix_version_from_text(raw)
            self.app_gui.append_ai_chat(
                f"🛠️ [Агент] Прямая команда: проставляю архитектуру для Story в релизе {release_key}\n"
            )
            result = self.app_gui.start_architecture_update_from_ai(
                release_key=release_key,
                announce_in_chat=True,
                forced_project_key=forced_project,
                forced_fix_version=forced_fix,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        move_if_ready_intent = any(
            phrase in lowered
            for phrase in ("move if ready", "сдвинь если готов", "переведи если готов", "двигай дальше")
        )
        if move_if_ready_intent and release_match:
            release_key = release_match.group(1).upper()
            dry_run = "dry-run" in lowered or "dry run" in lowered or "тестовый прогон" in lowered
            result = self.app_gui.move_release_if_ready(
                release_key=release_key,
                dry_run=dry_run,
                announce_in_chat=True,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        confirm_match = re.search(
            r"confirm_manual_check\s*\(\s*([A-Z]+-\d+)\s*,\s*([a-zA-Z0-9_]+)\s*,\s*(ok|fail|да|нет|true|false)\s*\)",
            raw,
            re.IGNORECASE,
        )
        if confirm_match:
            release_key = confirm_match.group(1).upper()
            check_id = confirm_match.group(2)
            result_flag = confirm_match.group(3).lower()
            result = self.app_gui.confirm_manual_check(
                release_key=release_key,
                check_id=check_id,
                result=result_flag,
                announce_in_chat=True,
            )
            self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
            return True

        bt_intent = any(
            phrase in lowered
            for phrase in (
                "бизнес-треб",
                "бт",
                "business requirements",
                "фр",
            )
        )
        project_candidates = re.findall(r"\b([A-Z][A-Z0-9_]{1,15})\b", raw)
        if bt_intent and release_match:
            release_key = release_match.group(1).upper()
            release_prefix = release_key.split("-")[0]
            project_key = ""
            for candidate in project_candidates:
                candidate_upper = candidate.upper()
                if candidate_upper != release_prefix and candidate_upper != release_key:
                    project_key = candidate_upper
                    break
            if project_key and project_key != release_key.split("-")[0]:
                self.app_gui.append_ai_chat(
                    f"🛠️ [Агент] Прямая команда: запуск bt3.py для {release_key}, проект {project_key}\n"
                )
                result = self.app_gui.start_business_requirements_from_ai(
                    release_key=release_key,
                    project_key=project_key,
                )
                self.app_gui.append_ai_chat(f"🤖 Blast AI: {result}\n\n")
                return True

            self.pending_bt_release_key = release_key
            self.app_gui.append_ai_chat(
                f"🤖 Blast AI: Для релиза {release_key} укажи project_key "
                "(например: SFILE, HRM, HRC, NEUROUI, SEARCHCS).\n\n"
            )
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
        self.guided_cycle_context: dict[str, dict] = {}

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
        self.sidebar = ctk.CTkFrame(self, width=SIDEBAR_WIDTH, corner_radius=0, fg_color=NEUTRAL_BG_SIDEBAR)
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
            text="Гайд",
            command=self.show_onboarding_manual,
            width=180,
            height=BUTTON_HEIGHT_PRIMARY,
            font=ctk.CTkFont(size=FONT_BODY, weight="bold"),
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
        )
        guide_btn.pack(pady=(SPACE_MD, SPACE_MD), padx=SPACE_SM)

        ctk.CTkLabel(self.sidebar, text="Blast", font=ctk.CTkFont(size=FONT_HEADING + 4, weight="bold"), text_color=NEUTRAL_TEXT).pack(pady=(SPACE_LG, SPACE_SM))
        ctk.CTkLabel(self.sidebar, text="v2.3 + AI", font=ctk.CTkFont(size=FONT_CAPTION), text_color=NEUTRAL_TEXT_SECONDARY).pack(pady=(0, SPACE_LG))

        # Кнопки навигации
        self.nav_btn_operations = ctk.CTkButton(
            self.sidebar, text="Операции", command=self.show_operations_tab,
            font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY,
        )
        self.nav_btn_operations.pack(pady=SPACE_SM, padx=SPACE_MD, fill="x")

        # === КНОПКА ИИ ===
        self.nav_btn_ai = ctk.CTkButton(
            self.sidebar, text="AI Помощник", command=self.show_ai_tab,
            font=ctk.CTkFont(size=FONT_BODY, weight="bold"), fg_color=PRIMARY, hover_color=PRIMARY_HOVER, height=BUTTON_HEIGHT_PRIMARY,
        )
        self.nav_btn_ai.pack(pady=SPACE_SM, padx=SPACE_MD, fill="x")

        self.nav_btn_history = ctk.CTkButton(
            self.sidebar, text="История", command=self.show_history_tab,
            font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY,
        )
        self.nav_btn_history.pack(pady=SPACE_SM, padx=SPACE_MD, fill="x")

        self.nav_btn_logs = ctk.CTkButton(
            self.sidebar, text="Логи", command=self.show_logs_tab,
            font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY,
        )
        self.nav_btn_logs.pack(pady=SPACE_SM, padx=SPACE_MD, fill="x")

        self.connection_label = ctk.CTkLabel(
            self.sidebar, text="● Проверка...",
            font=ctk.CTkFont(size=FONT_CAPTION), text_color=STATUS_PENDING,
        )
        self.connection_label.pack(side="bottom", pady=SPACE_MD)

        # Основная область
        self.main_content = ctk.CTkFrame(self, corner_radius=0, fg_color=NEUTRAL_BG_CARD)
        self.main_content.pack(side="right", fill="both", expand=True)

        self.create_operations_tab()
        self.create_ai_tab()
        self.create_history_tab()
        self.create_logs_tab()
        self.show_operations_tab()

    # === ИНТЕРФЕЙС ВКЛАДКИ ИИ ===
    def create_ai_tab(self):
        """Вкладка ИИ-Помощника"""
        self.ai_tab = ctk.CTkFrame(self.main_content, fg_color="transparent")

        header = ctk.CTkFrame(self.ai_tab, fg_color="transparent")
        header.pack(fill="x", padx=SPACE_MD, pady=(SPACE_MD, SPACE_SM))
        ctk.CTkLabel(header, text="Умный помощник", font=ctk.CTkFont(size=FONT_HEADING, weight="bold"), text_color=NEUTRAL_TEXT).pack(side="left")

        # Окно чата
        self.ai_chat_display = ctk.CTkTextbox(self.ai_tab, font=ctk.CTkFont(size=FONT_BODY), wrap="word", state="disabled", corner_radius=CORNER_RADIUS)
        self.ai_chat_display.pack(fill="both", expand=True, padx=SPACE_MD, pady=SPACE_SM)

        self.ai_chat_display.tag_config("ai_user", foreground=CHAT_USER, lmargin1=10, lmargin2=10)
        self.ai_chat_display.tag_config("ai_bot", foreground=CHAT_BOT, lmargin1=10, lmargin2=10)
        self.ai_chat_display.tag_config("ai_tool", foreground=CHAT_TOOL, lmargin1=10, lmargin2=10)
        self.ai_chat_display.tag_config("ai_error", foreground=CHAT_ERROR, lmargin1=10, lmargin2=10)
        self.ai_chat_display.tag_config("ai_default", foreground=CHAT_DEFAULT)

        # Приветственное сообщение
        self.append_ai_chat(
            "🤖 Blast AI: Готов к работе с релизом.\\n"
            "Примеры команд (подставь свой ключ релиза и fix version):\\n"
            "• Проверь статус HRPRELEASE-111135\\n"
            "• Привяжи задачи HM-REL-05-03-2026 в HRPRELEASE-111135\\n"
            "• Запусти полный цикл релиза для HRPRELEASE-111135\\n"
            "• Двигай HRPRELEASE-111135 дальше\\n"
            "• Проверь задачи и PR для HRPRELEASE-111135\\n"
            "• Проверь RQG для HRPRELEASE-111135\\n"
            "• Переведи HRPRELEASE-111135 в ПСИ\\n"
            "• Сделай БТ для HRPRELEASE-111135, проект SFILE\\n"
            "• Запусти полный пайплайн для HRPRELEASE-111135, проект SFILE\\n\\n"
        )

        quick_frame = ctk.CTkFrame(self.ai_tab, fg_color="transparent")
        quick_frame.pack(fill="x", padx=SPACE_MD, pady=(0, SPACE_SM))
        ctk.CTkLabel(
            quick_frame,
            text="Быстрые сценарии:",
            font=ctk.CTkFont(size=FONT_LABEL, weight="bold"),
            text_color=NEUTRAL_TEXT,
        ).pack(side="left", padx=(SPACE_SM, SPACE_SM), pady=SPACE_SM)

        ctk.CTkButton(
            quick_frame,
            text="Статус релиза",
            width=130,
            font=ctk.CTkFont(size=FONT_BODY),
            command=lambda: self.send_ai_quick_command("Проверь статус HRPRELEASE-"),
        ).pack(side="left", padx=SPACE_SM, pady=SPACE_SM)

        ctk.CTkButton(
            quick_frame,
            text="Полный пайплайн",
            width=150,
            font=ctk.CTkFont(size=FONT_BODY),
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            command=lambda: self.send_ai_quick_command(
                "Запусти полный пайплайн для HRPRELEASE-, проект SFILE, create_bt=true, create_deploy=true"
            ),
        ).pack(side="left", padx=SPACE_SM, pady=SPACE_SM)

        ctk.CTkButton(
            quick_frame,
            text="Сдвинуть статус",
            width=140,
            font=ctk.CTkFont(size=FONT_BODY),
            command=lambda: self.send_ai_quick_command(
                "Переведи HRPRELEASE- в Ready for Prod"
            ),
        ).pack(side="left", padx=SPACE_SM, pady=SPACE_SM)

        ctk.CTkButton(
            quick_frame,
            text="Задачи + PR",
            width=130,
            font=ctk.CTkFont(size=FONT_BODY),
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            command=lambda: self.send_ai_quick_command(
                "Проверь задачи и PR для HRPRELEASE-"
            ),
        ).pack(side="left", padx=SPACE_SM, pady=SPACE_SM)

        ctk.CTkButton(
            quick_frame,
            text="Guided cycle",
            width=130,
            font=ctk.CTkFont(size=FONT_BODY),
            fg_color=SEMANTIC_SUCCESS,
            hover_color=SEMANTIC_SUCCESS_HOVER,
            command=lambda: self.send_ai_quick_command(
                "Запусти полный цикл релиза для HRPRELEASE-"
            ),
        ).pack(side="left", padx=SPACE_SM, pady=SPACE_SM)

        # Ввод
        input_frame = ctk.CTkFrame(self.ai_tab, fg_color="transparent")
        input_frame.pack(fill="x", padx=SPACE_MD, pady=(0, SPACE_MD))

        self.ai_input = ctk.CTkEntry(
            input_frame,
            font=ctk.CTkFont(size=FONT_BODY),
            placeholder_text="Напр: Запусти полный пайплайн для HRPRELEASE-113300, project SFILE",
        )
        self.ai_input.pack(side="left", fill="x", expand=True, padx=(0, SPACE_SM))
        self.ai_input.bind("<Return>", self.send_ai_message)

        self.ai_send_btn = ctk.CTkButton(
            input_frame, text="Отправить", width=120, height=BUTTON_HEIGHT_PRIMARY,
            font=ctk.CTkFont(size=FONT_BODY, weight="bold"),
            fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
            command=self.send_ai_message,
        )
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

            self.ai_chat_display.insert("end", "\n" + text, tag)
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
        self.operations_tab = ctk.CTkFrame(self.main_content, fg_color="transparent")

        header = ctk.CTkFrame(self.operations_tab, fg_color="transparent")
        header.pack(fill="x", padx=SPACE_MD, pady=(SPACE_MD, SPACE_SM))
        ctk.CTkLabel(header, text="Операции с релизами", font=ctk.CTkFont(size=FONT_HEADING, weight="bold"), text_color=NEUTRAL_TEXT).pack(side="left")

        self.operations_tabs = ctk.CTkTabview(self.operations_tab)
        self.operations_tabs.pack(fill="both", expand=True, padx=SPACE_MD, pady=SPACE_SM)
        actions_tab = self.operations_tabs.add("Операции")
        monitor_tab = self.operations_tabs.add("Мониторинг")
        self.operations_tabs.set("Операции")

        input_frame = ctk.CTkFrame(actions_tab, corner_radius=CORNER_RADIUS)
        input_frame.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)
        input_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(input_frame, text="Ключ релиза:", font=ctk.CTkFont(size=FONT_LABEL, weight="bold"), text_color=NEUTRAL_TEXT).grid(row=0, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="e")
        self.release_entry = ctk.CTkEntry(
            input_frame,
            width=320,
            font=ctk.CTkFont(size=FONT_BODY),
            placeholder_text="HRPRELEASE-123456",
        )
        self.release_entry.grid(row=0, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        ctk.CTkLabel(input_frame, text="Fix Version:", font=ctk.CTkFont(size=FONT_LABEL, weight="bold"), text_color=NEUTRAL_TEXT).grid(row=1, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="e")
        self.version_entry = ctk.CTkEntry(
            input_frame,
            width=320,
            font=ctk.CTkFont(size=FONT_BODY),
            placeholder_text="Minor-2026-03-10",
        )
        self.version_entry.grid(row=1, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        ctk.CTkLabel(input_frame, text="Целевой LT (дни):", font=ctk.CTkFont(size=FONT_LABEL, weight="bold"), text_color=NEUTRAL_TEXT).grid(row=2, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="e")
        self.target_lt_entry = ctk.CTkEntry(input_frame, width=100, font=ctk.CTkFont(size=FONT_BODY))
        self.target_lt_entry.insert(0, "45")
        self.target_lt_entry.grid(row=2, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="w")

        status_panel = ctk.CTkFrame(actions_tab, corner_radius=CORNER_RADIUS)
        status_panel.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)
        status_panel.grid_columnconfigure(1, weight=1)
        status_panel.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            status_panel,
            text="Статус релиза:",
            font=ctk.CTkFont(size=FONT_LABEL, weight="bold"),
            text_color=NEUTRAL_TEXT,
        ).grid(row=0, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="e")

        self.release_status_value = ctk.CTkLabel(
            status_panel,
            text="не загружен",
            font=ctk.CTkFont(size=FONT_LABEL, weight="bold"),
            text_color=NEUTRAL_TEXT_SECONDARY,
        )
        self.release_status_value.grid(row=0, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="w")

        self.refresh_status_btn = ctk.CTkButton(
            status_panel,
            text="Обновить статус",
            width=140,
            font=ctk.CTkFont(size=FONT_BODY),
            command=self.refresh_release_status,
        )
        self.refresh_status_btn.grid(row=0, column=2, padx=SPACE_SM, pady=SPACE_SM, sticky="w")

        ctk.CTkLabel(
            status_panel,
            text="Новый статус:",
            font=ctk.CTkFont(size=FONT_LABEL, weight="bold"),
            text_color=NEUTRAL_TEXT,
        ).grid(row=1, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="e")

        self.target_status_entry = ctk.CTkEntry(
            status_panel,
            width=260,
            font=ctk.CTkFont(size=FONT_LABEL),
            placeholder_text="Напр: Ready for Prod",
        )
        self.target_status_entry.grid(row=1, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="w")

        self.move_status_btn = ctk.CTkButton(
            status_panel,
            text="Сдвинуть статус",
            width=140,
            font=ctk.CTkFont(size=FONT_BODY),
            command=self.move_release_status_manual,
        )
        self.move_status_btn.grid(row=1, column=2, padx=SPACE_SM, pady=SPACE_SM, sticky="w")

        self.ai_pipeline_btn = ctk.CTkButton(
            status_panel,
            text="Полный пайплайн",
            width=170,
            font=ctk.CTkFont(size=FONT_BODY),
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            command=self.run_ai_release_pipeline,
        )
        self.ai_pipeline_btn.grid(row=0, column=3, rowspan=2, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        options_frame = ctk.CTkFrame(actions_tab, corner_radius=CORNER_RADIUS)
        options_frame.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)

        self.dry_run_var = ctk.BooleanVar(value=False)
        self.dry_run_check = ctk.CTkCheckBox(options_frame, text="Тестовый прогон", variable=self.dry_run_var, font=ctk.CTkFont(size=FONT_LABEL))
        self.dry_run_check.pack(side="left", padx=SPACE_SM)

        self.parallel_var = ctk.BooleanVar(value=True)
        self.parallel_check = ctk.CTkCheckBox(options_frame, text="Параллельная обработка", variable=self.parallel_var, font=ctk.CTkFont(size=FONT_LABEL))
        self.parallel_check.pack(side="left", padx=SPACE_SM)

        buttons_frame = ctk.CTkFrame(actions_tab, corner_radius=CORNER_RADIUS)
        buttons_frame.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)

        self.link_btn = ctk.CTkButton(buttons_frame, text="Привязать задачи", command=self.link_issues, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY)
        self.link_btn.grid(row=0, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.cleanup_btn = ctk.CTkButton(buttons_frame, text="Очистить связи", command=self.cleanup_issues, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY)
        self.cleanup_btn.grid(row=0, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.remove_all_btn = ctk.CTkButton(buttons_frame, text="Удалить все связи", command=self.remove_all_issues, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY, fg_color=SEMANTIC_ERROR, hover_color=SEMANTIC_ERROR_HOVER)
        self.remove_all_btn.grid(row=0, column=2, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.lt_btn = ctk.CTkButton(buttons_frame, text="Проверка LT", command=self.run_lt_check, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY)
        self.lt_btn.grid(row=1, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.rqg_btn = ctk.CTkButton(
            buttons_frame,
            text="Проверка RQG",
            command=self.run_rqg_check,
            font=ctk.CTkFont(size=FONT_BODY),
            height=BUTTON_HEIGHT_PRIMARY,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
        )
        self.rqg_btn.grid(row=2, column=0, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.pr_status_btn = ctk.CTkButton(
            buttons_frame,
            text="Задачи + PR",
            command=self.run_release_pr_status_ui,
            font=ctk.CTkFont(size=FONT_BODY),
            height=BUTTON_HEIGHT_PRIMARY,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
        )
        self.pr_status_btn.grid(row=2, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.guided_cycle_btn = ctk.CTkButton(
            buttons_frame,
            text="Guided Cycle",
            command=self.run_guided_cycle_ui,
            font=ctk.CTkFont(size=FONT_BODY),
            height=BUTTON_HEIGHT_PRIMARY,
            fg_color=SEMANTIC_SUCCESS,
            hover_color=SEMANTIC_SUCCESS_HOVER,
        )
        self.guided_cycle_btn.grid(row=2, column=2, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.master_btn = ctk.CTkButton(buttons_frame, text="Мастер-ветки", command=self.analyze_master_services, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY, fg_color=SEMANTIC_SUCCESS, hover_color=SEMANTIC_SUCCESS_HOVER)
        self.master_btn.grid(row=1, column=1, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.deploy_btn = ctk.CTkButton(buttons_frame, text="Деплой-план", command=self.create_deploy_plan, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY, fg_color=SEMANTIC_WARNING, hover_color=SEMANTIC_WARNING_HOVER, state="disabled")
        self.deploy_btn.grid(row=1, column=2, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        self.cancel_btn = ctk.CTkButton(buttons_frame, text="Отменить", command=self.cancel_current_operation, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_PRIMARY, state="disabled")
        self.cancel_btn.grid(row=3, column=0, columnspan=4, padx=SPACE_SM, pady=SPACE_SM, sticky="ew")

        for i in range(4):
            buttons_frame.columnconfigure(i, weight=1)

        # Информация о Confluence
        if CONFLUENCE_SPACE_KEY and CONFLUENCE_PARENT_PAGE_TITLE:
            info_label = ctk.CTkLabel(
                actions_tab,
                text=f"Confluence: {CONFLUENCE_SPACE_KEY}/{CONFLUENCE_PARENT_PAGE_TITLE} | Команда: {TEAM_NAME}",
                font=ctk.CTkFont(size=FONT_CAPTION),
                text_color=NEUTRAL_TEXT_SECONDARY,
            )
            info_label.pack(pady=SPACE_SM)

        progress_frame = ctk.CTkFrame(monitor_tab, corner_radius=CORNER_RADIUS)
        progress_frame.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)

        self.progress_label = ctk.CTkLabel(
            progress_frame,
            text="Ожидание запуска...",
            font=ctk.CTkFont(size=FONT_SUBHEADING, weight="bold"),
            text_color=NEUTRAL_TEXT_SECONDARY,
        )
        self.progress_label.pack(pady=SPACE_SM)

        self.progress_bar = ctk.CTkProgressBar(progress_frame, width=600)
        self.progress_bar.pack(pady=SPACE_SM)
        self.progress_bar.set(0)

        self.details_label = ctk.CTkLabel(progress_frame, text="", font=ctk.CTkFont(size=FONT_CAPTION), text_color=NEUTRAL_TEXT_MUTED)
        self.details_label.pack(pady=SPACE_SM)

        results_frame = ctk.CTkFrame(monitor_tab, corner_radius=CORNER_RADIUS)
        results_frame.pack(fill="both", expand=True, padx=SPACE_SM, pady=SPACE_SM)

        ctk.CTkLabel(results_frame, text="Результаты:", font=ctk.CTkFont(size=FONT_LABEL, weight="bold"), text_color=NEUTRAL_TEXT).pack(anchor="w", padx=SPACE_SM, pady=SPACE_SM)

        self.results_text = ctk.CTkTextbox(results_frame, font=ctk.CTkFont(size=FONT_MONO), wrap="word", corner_radius=CORNER_RADIUS)
        self.results_text.pack(fill="both", expand=True, padx=SPACE_SM, pady=SPACE_SM)
        self.results_text.tag_config("success", foreground=SEMANTIC_SUCCESS)
        self.results_text.tag_config("error", foreground=SEMANTIC_ERROR)
        self.results_text.tag_config("warning", foreground=SEMANTIC_WARNING)
        self.results_text.tag_config("info", foreground=SEMANTIC_INFO)

    def show_onboarding_manual(self):
        """Показать онбординг вручную"""
        from onboarding import OnboardingWindow
        OnboardingWindow(self)

    def create_history_tab(self):
        """Вкладка истории"""
        self.history_tab = ctk.CTkFrame(self.main_content, fg_color="transparent")

        header = ctk.CTkFrame(self.history_tab, fg_color="transparent")
        header.pack(fill="x", padx=SPACE_MD, pady=SPACE_MD)

        ctk.CTkLabel(header, text="История операций", font=ctk.CTkFont(size=FONT_HEADING, weight="bold"), text_color=NEUTRAL_TEXT).pack(side="left")
        ctk.CTkButton(header, text="Обновить", command=self.refresh_history, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_SECONDARY).pack(side="right", padx=SPACE_SM)
        ctk.CTkButton(header, text="Очистить", command=self.clear_history, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_SECONDARY).pack(side="right", padx=SPACE_SM)

        self.history_text = ctk.CTkTextbox(self.history_tab, font=ctk.CTkFont(size=FONT_MONO), corner_radius=CORNER_RADIUS)
        self.history_text.pack(fill="both", expand=True, padx=SPACE_MD, pady=SPACE_SM)

    def create_logs_tab(self):
        """Вкладка логов"""
        self.logs_tab = ctk.CTkFrame(self.main_content, fg_color="transparent")

        header = ctk.CTkFrame(self.logs_tab, fg_color="transparent")
        header.pack(fill="x", padx=SPACE_MD, pady=SPACE_MD)

        ctk.CTkLabel(header, text="Логи приложения", font=ctk.CTkFont(size=FONT_HEADING, weight="bold"), text_color=NEUTRAL_TEXT).pack(side="left")
        ctk.CTkButton(header, text="Обновить", command=self.refresh_logs, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_SECONDARY).pack(side="right", padx=SPACE_SM)
        ctk.CTkButton(header, text="Очистить", command=self.clear_logs, font=ctk.CTkFont(size=FONT_BODY), height=BUTTON_HEIGHT_SECONDARY).pack(side="right", padx=SPACE_SM)

        self.log_text = ctk.CTkTextbox(self.logs_tab, font=ctk.CTkFont(size=FONT_MONO), corner_radius=CORNER_RADIUS)
        self.log_text.pack(fill="both", expand=True, padx=SPACE_MD, pady=SPACE_SM)
        self.log_text.tag_config("log_error", foreground=SEMANTIC_ERROR)
        self.log_text.tag_config("log_warning", foreground=SEMANTIC_WARNING)
        self.log_text.tag_config("log_info", foreground=SEMANTIC_INFO)

    def hide_all_tabs(self):
        """Скрыть все вкладки"""
        self.operations_tab.pack_forget()
        self.ai_tab.pack_forget()
        self.history_tab.pack_forget()
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
                    self.connection_label.configure(text="● Подключено", text_color=STATUS_OK)
                else:
                    self.connection_label.configure(text="● Ошибка", text_color=STATUS_ERROR)
            self.after(0, update_ui)
        threading.Thread(target=check, daemon=True).start()

    def _release_status_color(self, status_name: str) -> str:
        status = (status_name or "").strip().lower()
        if any(token in status for token in ("done", "ready", "resolved", "closed", "готов", "закрыт")):
            return SEMANTIC_SUCCESS
        if any(token in status for token in ("blocked", "error", "failed", "отклон", "ошиб", "rejected")):
            return SEMANTIC_ERROR
        if any(token in status for token in ("progress", "review", "тест", "в работе")):
            return SEMANTIC_WARNING
        return PRIMARY

    def refresh_release_status(self):
        release_key = self.release_entry.get().strip().upper()
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return
        issue = self.jira_service.get_issue_details(release_key)
        if not issue:
            self.release_status_value.configure(text="не найден", text_color=STATUS_ERROR)
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

    def start_business_requirements_from_ai(self, release_key: str, project_key: str) -> str:
        """Запуск bt3.py из AI-чата после получения project_key."""
        safe_release = (release_key or "").strip().upper()
        safe_project = (project_key or "").strip().upper()
        if not safe_release:
            return "Ошибка: release_key не указан."
        if not safe_project:
            return "Ошибка: project_key не указан."

        self.after(0, self._open_monitor_tab)
        self.after(0, lambda: self.update_status("Запуск генерации бизнес-требований..."))
        self.after(0, lambda: self.details_label.configure(text=f"{safe_release} / {safe_project}"))
        self.after(0, lambda: self.progress_bar.set(0.1))

        worker = threading.Thread(
            target=self._business_requirements_thread,
            args=(safe_release, safe_project, True),
            daemon=True,
        )
        worker.start()
        return (
            f"Запускаю bt3.py для релиза {safe_release} и проекта {safe_project}. "
            "Результат пришлю в чат."
        )

    def _business_requirements_thread(self, release_key: str, project_key: str, announce_in_chat: bool):
        try:
            script_path = os.path.join(os.path.dirname(__file__), "bt3.py")
            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Скрипт не найден: {script_path}")

            process = subprocess.run(
                [sys.executable, script_path, release_key, project_key],
                capture_output=True,
                text=True,
                check=False,
            )
            output = process.stdout or ""
            stderr = process.stderr or ""

            self.after(0, lambda: self.progress_bar.set(1.0))
            if "ok=True" in output:
                url_match = re.search(r"url=(https?://[^\s]+)", output)
                url = url_match.group(1) if url_match else "Ссылка не найдена"
                self.after(0, lambda: self.update_status("Готово"))
                self.after(0, lambda: self.add_result(f"✅ Бизнес-требования созданы: {url}"))
                self.history.add("Создание БТ/ФР", {"release": release_key, "project_key": project_key, "url": url})
                self.history.save_to_file(self.history_path)
                if announce_in_chat:
                    self.append_ai_chat(
                        f"🤖 Blast AI: Бизнес-требования для {release_key} ({project_key}) успешно созданы.\n"
                        f"Ссылка: {url}\n\n"
                    )
            else:
                error_text = f"Ошибка bt3.py.\nSTDOUT: {output}\nSTDERR: {stderr}"
                self.after(0, lambda: self.update_status("Ошибка"))
                self.after(0, lambda e=error_text: self.add_result(f"❌ {e}"))
                if announce_in_chat:
                    self.append_ai_chat(f"⚠️ Ошибка генерации бизнес-требований:\n{error_text}\n\n")
        except Exception as e:
            self.after(0, lambda: self.update_status("Ошибка"))
            self.after(0, lambda: self.add_result(f"❌ Ошибка запуска bt3.py: {e}"))
            if announce_in_chat:
                self.append_ai_chat(f"⚠️ Ошибка запуска bt3.py: {e}\n\n")

    def start_architecture_update_from_ai(
        self,
        release_key: str,
        announce_in_chat: bool = False,
        forced_project_key: str = "",
        forced_fix_version: str = "",
    ) -> str:
        safe_release = (release_key or "").strip().upper()
        if not safe_release:
            return "Ошибка: release_key не указан."
        safe_project = (forced_project_key or "").strip().upper()
        safe_fix = (forced_fix_version or "").strip()

        self.after(0, self._open_monitor_tab)
        self.after(0, lambda: self.update_status("Проставление архитектуры..."))
        details = f"Релиз: {safe_release}"
        if safe_project and safe_fix:
            details += f" | override: {safe_project}/{safe_fix}"
        self.after(0, lambda d=details: self.details_label.configure(text=d))
        self.after(0, lambda: self.progress_bar.set(0.05))

        worker = threading.Thread(
            target=self._architecture_update_thread,
            args=(safe_release, announce_in_chat, safe_project, safe_fix),
            daemon=True,
        )
        worker.start()
        return (
            f"Запускаю проставление архитектуры для Story в релизе {safe_release}. "
            "Прогресс в Мониторинге."
        )

    def _architecture_update_thread(
        self,
        release_key: str,
        announce_in_chat: bool,
        forced_project_key: str = "",
        forced_fix_version: str = "",
    ):
        try:
            if not ARCH_JIRA_TOKEN:
                raise ValueError("Не найден JIRA_TOKEN для arch.py логики.")

            release = self.jira_service.get_issue_details(release_key)
            if not release:
                raise ValueError(f"Релиз {release_key} не найден.")
            self.after(0, lambda: self.progress_bar.set(0.15))

            # Новая логика: берем ЛЮБУЮ Story из состава релиза и используем ее project+fixVersion.
            project_key = (forced_project_key or "").strip().upper()
            fix_version = (forced_fix_version or "").strip()

            linked_keys = self.jira_service.get_linked_issues(release_key)
            for idx, key in enumerate(linked_keys, start=1):
                issue = self.jira_service.get_issue_details(key)
                if not issue:
                    continue
                issue_type = (issue.get("fields", {}).get("issuetype", {}).get("name") or "").lower()
                if issue_type != "story":
                    continue

                if not project_key:
                    project_key = (issue.get("fields", {}).get("project", {}).get("key") or "").strip().upper()
                if not fix_version:
                    for fv in issue.get("fields", {}).get("fixVersions", []) or []:
                        name = (fv.get("name") or "").strip()
                        if name and not re.fullmatch(r"HRPRELEASE-\d+", name, re.IGNORECASE):
                            fix_version = name
                            break

                self.after(
                    0,
                    lambda p=min(0.45, 0.15 + idx / max(len(linked_keys), 1) * 0.3): self.progress_bar.set(p),
                )
                if project_key and fix_version:
                    break

            # Fallback: project из consists-of, если Story не дала project.
            if not project_key:
                for link in release.get("fields", {}).get("issuelinks", []) or []:
                    link_type = link.get("type", {}) or {}
                    outward_name = (link_type.get("outward") or "").lower()
                    inward_name = (link_type.get("inward") or "").lower()
                    if "consists of" not in outward_name and "consists of" not in inward_name:
                        continue
                    target = link.get("outwardIssue") or link.get("inwardIssue") or {}
                    key = (target.get("key") or "").strip().upper()
                    if "-" in key:
                        project_key = key.split("-", 1)[0]
                        break

            # Fallback: fixVersion из поля релиза или UI.
            if not fix_version:
                for fv in release.get("fields", {}).get("fixVersions", []) or []:
                    name = (fv.get("name") or "").strip()
                    if name and not re.fullmatch(r"HRPRELEASE-\d+", name, re.IGNORECASE):
                        fix_version = name
                        break
            if not fix_version:
                ui_fix = (self.version_entry.get() or "").strip()
                if ui_fix and not re.fullmatch(r"HRPRELEASE-\d+", ui_fix, re.IGNORECASE):
                    fix_version = ui_fix

            if not project_key or not fix_version:
                self.ai_assistant.pending_arch_release_key = release_key
                missing = []
                if not project_key:
                    missing.append("project_key")
                if not fix_version:
                    missing.append("fix_version")
                raise ValueError(
                    "Не удалось определить " + ", ".join(missing) + ". "
                    "Пришли уточнение, например: 'HRC WEB-2026.03.X' "
                    "или 'project HRC, fixVersion WEB-2026.03.X'."
                )

            self.after(
                0,
                lambda pk=project_key, fv=fix_version: self.details_label.configure(
                    text=f"Запуск arch.py: {pk} / {fv}"
                ),
            )
            self.after(0, lambda: self.progress_bar.set(0.55))

            script_path = os.path.join(os.path.dirname(__file__), "arch.py")
            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Скрипт не найден: {script_path}")

            process = subprocess.run(
                [
                    sys.executable,
                    script_path,
                    "--project-key",
                    project_key,
                    "--fix-version",
                    fix_version,
                    "--yes",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            stdout = process.stdout or ""
            stderr = process.stderr or ""
            self.after(0, lambda: self.progress_bar.set(1.0))

            if process.returncode != 0:
                raise RuntimeError(
                    "arch.py завершился с ошибкой.\n"
                    f"STDOUT:\n{stdout[-3000:]}\n\nSTDERR:\n{stderr[-3000:]}"
                )

            fixed_match = re.search(r"Успешно установлено значение:\s*(\d+)", stdout)
            fixed_count = int(fixed_match.group(1)) if fixed_match else None
            summary = (
                f"Архитектура проставлена для релиза {release_key}: "
                f"{project_key}/{fix_version}"
            )
            if fixed_count is not None:
                summary += f", успешно: {fixed_count}"

            self.after(0, lambda: self.update_status("Готово"))
            self.after(0, lambda s=summary: self.add_result(f"✅ {s}"))
            self.history.add(
                "Архитектура Story",
                {"release": release_key, "project_key": project_key, "fix_version": fix_version},
            )
            self.history.save_to_file(self.history_path)

            if announce_in_chat:
                self.append_ai_chat(
                    f"🤖 Blast AI: {summary}\n\n"
                )
        except Exception as e:
            error_text = f"Ошибка проставления архитектуры: {e}"
            self.after(0, lambda: self.update_status("Ошибка"))
            self.after(0, lambda: self.add_result(f"❌ {error_text}"))
            if announce_in_chat:
                self.append_ai_chat(f"⚠️ {error_text}\n\n")

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

    def run_guided_cycle_ui(self):
        release_key = self.release_entry.get().strip().upper()
        if not release_key:
            messagebox.showwarning("Ошибка", "Введите ключ релиза!")
            return
        self.start_release_guided_cycle(
            release_key=release_key,
            profile="auto",
            dry_run=bool(self.dry_run_var.get()),
            announce_in_chat=False,
        )

    def start_release_guided_cycle(
        self,
        release_key: str,
        profile: str = "auto",
        dry_run: bool = False,
        announce_in_chat: bool = False,
    ) -> str:
        safe_release = (release_key or "").strip().upper()
        safe_profile = (profile or "auto").strip().lower()
        if not safe_release:
            return "Ошибка: release_key не указан."

        self.after(0, self._open_monitor_tab)
        self.after(0, lambda: self.results_text.delete("1.0", "end"))
        self.after(0, lambda: self.progress_bar.set(0))
        self.after(0, lambda: self.update_status(f"Guided cycle: {safe_release}"))
        self.after(0, lambda: self.details_label.configure(text=f"Профиль: {safe_profile} | dry_run={dry_run}"))

        worker = threading.Thread(
            target=self._guided_cycle_thread,
            args=(safe_release, safe_profile, dry_run, announce_in_chat),
            daemon=True,
        )
        worker.start()
        return (
            f"Запущен guided cycle для {safe_release} (profile={safe_profile}, dry_run={dry_run}). "
            "Результаты и блокировки будут показаны в Мониторинге."
        )

    def _guided_cycle_thread(self, release_key: str, profile: str, dry_run: bool, announce_in_chat: bool):
        try:
            result = evaluate_release_gates(
                jira_service=self.jira_service,
                release_key=release_key,
                profile_name=profile,
                manual_confirmations=(self.guided_cycle_context.get(release_key, {}) or {}).get("manual_confirmations"),
            )
            report = format_release_gate_report(result)

            def show_report():
                self.results_text.delete("1.0", "end")
                self.results_text.insert("1.0", report)
                self.update_progress(1.0, "Оценка гейтов завершена")
                self.update_status("Готово" if result.get("success") else "Ошибка")

            self.after(0, show_report)
            if result.get("success"):
                self.guided_cycle_context[release_key] = {
                    "profile": result.get("profile_name", profile),
                    "dry_run": dry_run,
                    "last_result": result,
                    "manual_confirmations": (
                        self.guided_cycle_context.get(release_key, {}) or {}
                    ).get("manual_confirmations", {}),
                }
                self.history.add(
                    "Guided cycle",
                    {
                        "release": release_key,
                        "profile": result.get("profile_name", profile),
                        "dry_run": dry_run,
                        "ready_for_transition": result.get("ready_for_transition", False),
                    },
                )
                self.history.save_to_file(self.history_path)

            if announce_in_chat:
                self.append_ai_chat(
                    f"🤖 Blast AI: Guided cycle для {release_key} завершен.\n{report}\n\n"
                )
        except Exception as e:
            error_text = f"Ошибка guided cycle: {e}"
            self.after(0, lambda: self.update_status("Ошибка"))
            self.after(0, lambda: self.add_result(f"❌ {error_text}"))
            if announce_in_chat:
                self.append_ai_chat(f"⚠️ {error_text}\n\n")

    def run_next_release_step(self, release_key: str, dry_run: bool = False, announce_in_chat: bool = False) -> str:
        safe_release = (release_key or "").strip().upper()
        if not safe_release:
            return "Ошибка: release_key не указан."
        context = self.guided_cycle_context.get(safe_release, {})
        profile = context.get("profile", "auto")
        effective_dry_run = context.get("dry_run", dry_run)
        return self.start_release_guided_cycle(
            release_key=safe_release,
            profile=profile,
            dry_run=effective_dry_run,
            announce_in_chat=announce_in_chat,
        )

    def confirm_manual_check(
        self,
        release_key: str,
        check_id: str,
        result: str,
        announce_in_chat: bool = False,
    ) -> str:
        safe_release = (release_key or "").strip().upper()
        safe_check = (check_id or "").strip()
        verdict = (result or "").strip().lower()
        if not safe_release or not safe_check:
            return "Ошибка: release_key/check_id обязательны."

        ok_values = {"ok", "true", "yes", "да"}
        is_ok = verdict in ok_values

        context = self.guided_cycle_context.setdefault(
            safe_release,
            {"profile": "auto", "manual_confirmations": {}, "last_result": None},
        )
        confirmations = context.setdefault("manual_confirmations", {})
        confirmations[safe_check] = is_ok
        profile = context.get("profile", "auto")

        message = (
            f"Подтверждение '{safe_check}' сохранено как {'OK' if is_ok else 'FAIL'} "
            f"для {safe_release}. Переоцениваю гейты..."
        )
        self.start_release_guided_cycle(
            release_key=safe_release,
            profile=profile,
            announce_in_chat=announce_in_chat,
        )
        return message

    def move_release_if_ready(self, release_key: str, dry_run: bool = False, announce_in_chat: bool = False) -> str:
        safe_release = (release_key or "").strip().upper()
        if not safe_release:
            return "Ошибка: release_key не указан."

        context = self.guided_cycle_context.get(safe_release)
        if not context or not context.get("last_result"):
            result = evaluate_release_gates(self.jira_service, safe_release, "auto")
            context = {
                "profile": result.get("profile_name", "auto"),
                "dry_run": dry_run,
                "last_result": result,
                "manual_confirmations": {},
            }
            self.guided_cycle_context[safe_release] = context

        last_result = context.get("last_result") or {}
        if not last_result.get("ready_for_transition"):
            report = format_release_gate_report(last_result)
            return (
                "Переход заблокирован: не пройдены все гейты.\n"
                f"{report}\n"
                f"Подтверди ручной чек: confirm_manual_check({safe_release}, <check_id>, ok)"
            )

        next_status = last_result.get("next_allowed_transition")
        next_transition_id = last_result.get("next_allowed_transition_id")
        if not next_status:
            return "Релиз уже в финальном статусе или следующий этап не определен."

        effective_dry_run = context.get("dry_run", dry_run)
        if effective_dry_run:
            return (
                f"[DRY-RUN] Релиз {safe_release} готов к переходу в '{next_status}'"
                + (f" (transition id: {next_transition_id})" if next_transition_id else "")
                + ". Фактический перевод не выполнен."
            )

        if next_transition_id:
            ok, msg = self.jira_service.transition_issue_by_id(safe_release, next_transition_id)
        else:
            ok, msg = self.jira_service.transition_issue(safe_release, next_status)
        if not ok:
            return f"Не удалось перевести релиз: {msg}"

        self.history.add(
            "Guided transition",
            {"release": safe_release, "target_status": next_status},
        )
        self.history.save_to_file(self.history_path)

        # После перевода сразу переоцениваем следующий шаг.
        self.start_release_guided_cycle(
            release_key=safe_release,
            profile=context.get("profile", "auto"),
            dry_run=effective_dry_run,
            announce_in_chat=announce_in_chat,
        )
        return f"Релиз {safe_release} переведен в '{next_status}'. Переоцениваю следующий шаг."

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
            projects = ", ".join(p.strip() for p in LINK_TASKS_PROJECTS.split(",") if p.strip())
            jql = f'project IN ({projects}) AND issuetype IN (Bug, Story) AND fixVersion = "{fix_version}"'
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
        color = PRIMARY
        lowered = text.lower()
        if "ошиб" in lowered or "error" in lowered:
            color = SEMANTIC_ERROR
        elif "готов" in lowered or "успеш" in lowered or "done" in lowered:
            color = SEMANTIC_SUCCESS
        elif "отмена" in lowered or "warning" in lowered:
            color = SEMANTIC_WARNING
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
        self.guided_cycle_btn.configure(state=state)
        self.master_btn.configure(state=state)
        self.release_entry.configure(state=state)
        self.version_entry.configure(state=state)


if __name__ == "__main__":
    app = ModernJiraApp()
    app.mainloop()