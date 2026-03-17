from __future__ import annotations

import sys
from pathlib import Path

from agent_automatic.app.container import build_container
from agent_automatic.app.settings import load_settings
from agent_automatic.interfaces.cli.main import run_cli


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    root = Path(__file__).resolve().parents[2]
    settings = load_settings(root)
    container = build_container(settings)
    return run_cli(container.chat_controller, argv)


if __name__ == "__main__":
    raise SystemExit(main())

