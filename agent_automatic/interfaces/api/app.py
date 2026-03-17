from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from pydantic import BaseModel

from agent_automatic.interfaces.ui.controllers import ChatController


class CommandRequest(BaseModel):
    text: str
    conversation_id: str = "default"


class CommandResponse(BaseModel):
    output: str


@dataclass(frozen=True, slots=True)
class ApiApp:
    app: FastAPI


def create_app(controller: ChatController) -> FastAPI:
    app = FastAPI(title="agent_automatic")

    @app.post("/command", response_model=CommandResponse)
    def run_command(req: CommandRequest):
        out = controller.handle_text(text=req.text, conversation_id=req.conversation_id)
        return CommandResponse(output=out)

    return app

