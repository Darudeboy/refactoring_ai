from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from agent_automatic.interfaces.ui.controllers import ChatController


class CommandRequest(BaseModel):
    text: str
    conversation_id: str = "default"


class CommandResponse(BaseModel):
    output: str


def create_app(controller: ChatController) -> FastAPI:
    app = FastAPI(title="agent_automatic")
    templates_dir = Path(__file__).resolve().parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    @app.get("/", response_class=HTMLResponse)
    def form_page(request: Request):
        return templates.TemplateResponse(
            "form.html",
            {"request": request, "text": "", "result": None},
        )

    @app.post("/", response_class=HTMLResponse)
    def run_from_form(
        request: Request,
        text: str = Form(..., description="Команда"),
        conversation_id: str = Form("default"),
    ):
        out = controller.handle_text(text=text.strip(), conversation_id=conversation_id)
        return templates.TemplateResponse(
            "form.html",
            {"request": request, "text": text, "result": out if out else ""},
        )

    @app.post("/command", response_model=CommandResponse)
    def run_command(req: CommandRequest):
        out = controller.handle_text(text=req.text, conversation_id=req.conversation_id)
        return CommandResponse(output=out)

    return app

