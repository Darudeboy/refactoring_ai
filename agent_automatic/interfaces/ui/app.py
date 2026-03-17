from __future__ import annotations

"""
UI integration point.

This module intentionally contains no workflow logic; it wires a controller that can be embedded
into Tk/CustomTkinter UI or any other UI surface.
"""

from agent_automatic.interfaces.ui.controllers import ChatController


class UiApp:
    def __init__(self, controller: ChatController):
        self.controller = controller

    def handle_user_message(self, text: str, *, conversation_id: str = "default") -> str:
        return self.controller.handle_text(text=text, conversation_id=conversation_id)

