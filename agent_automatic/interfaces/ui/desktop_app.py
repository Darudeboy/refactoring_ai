"""
Десктопное приложение: одно окно, поле команды, кнопка «Выполнить», вывод результата.
Запуск: python -m agent_automatic.interfaces.ui.desktop_app
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext


def main() -> int:
    from agent_automatic.app.container import build_container
    from agent_automatic.app.settings import load_settings

    root_path = Path(__file__).resolve().parents[3]
    settings = load_settings(root_path)
    container = build_container(settings)
    controller = container.chat_controller

    app = tk.Tk()
    app.title("Релиз — команды")
    app.minsize(480, 320)
    app.geometry("620x420")

    # Поле ввода
    frame_in = tk.Frame(app, padx=10, pady=10)
    frame_in.pack(fill=tk.BOTH, expand=False)
    tk.Label(frame_in, text="Команда", font=("", 11)).pack(anchor=tk.W)
    entry = tk.Text(frame_in, height=3, wrap=tk.WORD, font=("", 11), padx=6, pady=6)
    entry.pack(fill=tk.X, pady=(4, 8))
    entry.insert(tk.END, "привяжи задачи HM-REL-05-03-2026 в HRPRELEASE-123456")
    entry.focus_set()

    # Кнопка
    def run_cmd():
        text = entry.get("1.0", tk.END).strip()
        if not text:
            result_area.delete("1.0", tk.END)
            result_area.insert(tk.END, "Введите команду.")
            return
        result_area.delete("1.0", tk.END)
        result_area.insert(tk.END, "Выполняю…")
        app.update()
        out = controller.handle_text(text=text, conversation_id="default")
        result_area.delete("1.0", tk.END)
        result_area.insert(tk.END, out if out else "—")
        result_area.see(tk.END)

    btn = tk.Button(frame_in, text="Выполнить", command=run_cmd, font=("", 11), padx=12, pady=6)
    btn.pack(anchor=tk.W)

    # Результат
    frame_out = tk.Frame(app, padx=10, pady=10)
    frame_out.pack(fill=tk.BOTH, expand=True)
    tk.Label(frame_out, text="Результат", font=("", 11)).pack(anchor=tk.W)
    result_area = scrolledtext.ScrolledText(
        frame_out, wrap=tk.WORD, font=("", 10), padx=6, pady=6, state=tk.NORMAL
    )
    result_area.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
