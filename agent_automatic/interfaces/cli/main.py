from __future__ import annotations

import argparse

from agent_automatic.interfaces.ui.controllers import ChatController


def run_cli(controller: ChatController, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent_automatic")
    parser.add_argument("--conversation-id", default="default")
    parser.add_argument("--text", help="One-shot command text")
    args = parser.parse_args(argv)

    if args.text:
        out = controller.handle_text(text=args.text, conversation_id=args.conversation_id)
        if out:
            print(out)
        return 0

    # Interactive REPL
    print("agent_automatic CLI. Type 'exit' to quit.")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line.lower() in {"exit", "quit"}:
            break
        out = controller.handle_text(text=line, conversation_id=args.conversation_id)
        if out:
            print(out)
    return 0

