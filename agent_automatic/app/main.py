from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    root = Path(__file__).resolve().parents[2]

    if "--api" in argv:
        argv = [a for a in argv if a != "--api"]
        from agent_automatic.app.container import build_container
        from agent_automatic.app.settings import load_settings
        from agent_automatic.interfaces.api.app import create_app
        import uvicorn
        settings = load_settings(root)
        container = build_container(settings)
        app = create_app(container.chat_controller)
        uvicorn.run(app, host="127.0.0.1", port=8000)
        return 0

    if "--cli" in argv:
        argv = [a for a in argv if a != "--cli"]
        from agent_automatic.app.container import build_container
        from agent_automatic.app.settings import load_settings
        from agent_automatic.interfaces.cli.main import run_cli
        settings = load_settings(root)
        container = build_container(settings)
        return run_cli(container.chat_controller, argv)

    # По умолчанию — десктопное приложение
    from agent_automatic.interfaces.ui.desktop_app import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())

