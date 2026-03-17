from __future__ import annotations

from pathlib import Path

import uvicorn

from agent_automatic.app.container import build_container
from agent_automatic.app.settings import load_settings
from agent_automatic.interfaces.api.app import create_app


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    settings = load_settings(root)
    container = build_container(settings)
    app = create_app(container.chat_controller)
    uvicorn.run(app, host="127.0.0.1", port=8000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

