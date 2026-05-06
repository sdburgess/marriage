"""FastAPI app: HTML dashboard + Slack interactivity webhook.

Routes:

    GET  /                  -- HTML dashboard (basic auth)
    POST /pause             -- pause the scheduler
    POST /resume            -- resume the scheduler
    POST /reload-config     -- re-read config.yaml from disk
    POST /save-config       -- write new YAML to disk + reload
    POST /slack/actions     -- Slack interactivity webhook (Book/Skip)
    GET  /healthz           -- health check (no auth)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets as _secrets
import time
from pathlib import Path
from typing import Annotated

import structlog
import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .config import Config
from .controller import Controller

log = structlog.get_logger()

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_basic = HTTPBasic()


def build_app(controller: Controller) -> FastAPI:
    app = FastAPI(title="cupid", docs_url=None, redoc_url=None)

    def _check_auth(creds: Annotated[HTTPBasicCredentials, Depends(_basic)]) -> str:
        s = controller.secrets
        if not s.web_username or not s.web_password:
            # If no creds configured, lock the dashboard down hard rather
            # than expose it open to the world.
            raise HTTPException(
                status_code=503,
                detail="dashboard auth not configured (set WEB_USERNAME/WEB_PASSWORD)",
            )
        ok_user = _secrets.compare_digest(creds.username, s.web_username)
        ok_pass = _secrets.compare_digest(creds.password, s.web_password)
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=401,
                detail="bad creds",
                headers={"WWW-Authenticate": "Basic"},
            )
        return creds.username

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "paused": controller.paused}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, _: str = Depends(_check_auth)) -> Response:
        cfg = controller.cfg
        st = controller.state
        config_yaml_text = _read_config_file(controller.config_path)
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "paused": controller.paused,
                "cfg": cfg,
                "targets": cfg.targets,
                "polling": cfg.polling,
                "server_cfg": cfg.server,
                "bookings": st._data.get("bookings", []),
                "last_run_at": st._data.get("last_run_at"),
                "seen_count": len(st.seen_slots),
                "activity": list(controller.activity)[:50],
                "pending": controller.list_pending(),
                "config_yaml": config_yaml_text,
            },
        )

    @app.post("/pause")
    def pause(_: str = Depends(_check_auth)) -> RedirectResponse:
        controller.pause()
        return RedirectResponse("/", status_code=303)

    @app.post("/resume")
    def resume(_: str = Depends(_check_auth)) -> RedirectResponse:
        controller.resume()
        return RedirectResponse("/", status_code=303)

    @app.post("/reload-config")
    async def reload_config(_: str = Depends(_check_auth)) -> RedirectResponse:
        try:
            await controller.reload_config()
        except Exception as e:
            controller.log_activity("error", f"reload failed: {e}")
        return RedirectResponse("/", status_code=303)

    @app.post("/save-config")
    async def save_config(
        config_yaml: Annotated[str, Form()],
        _: str = Depends(_check_auth),
    ) -> RedirectResponse:
        try:
            parsed = yaml.safe_load(config_yaml)
            Config.model_validate(parsed)         # validate before writing
        except Exception as e:
            controller.log_activity("error", f"invalid config: {e}")
            return RedirectResponse("/?err=invalid_config", status_code=303)
        Path(controller.config_path).write_text(config_yaml)
        await controller.reload_config()
        return RedirectResponse("/", status_code=303)

    @app.post("/slack/actions")
    async def slack_actions(request: Request) -> Response:
        body = await request.body()
        if not _verify_slack_signature(
            controller.secrets.slack_signing_secret,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            body,
            request.headers.get("X-Slack-Signature", ""),
        ):
            log.warning("slack.signature_failed")
            raise HTTPException(status_code=401, detail="bad signature")

        # Slack sends payload as form-encoded `payload=<json>`
        form = await request.form()
        payload = json.loads(str(form.get("payload", "{}")))
        actions = payload.get("actions", [])
        if not actions:
            return Response(status_code=200)
        action = actions[0]
        action_id = action.get("action_id")
        callback_id = action.get("value")
        if action_id not in {"book", "skip"} or not callback_id:
            return Response(status_code=200)

        decision = "book" if action_id == "book" else "skip"
        ok = controller.resolve_pending(callback_id, decision)
        controller.log_activity(
            "info",
            f"slack action: {action_id}",
            user=payload.get("user", {}).get("username"),
            resolved=ok,
        )
        return Response(status_code=200)

    return app


def _read_config_file(path: str) -> str:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def _verify_slack_signature(
    signing_secret: str, timestamp: str, body: bytes, signature: str
) -> bool:
    if not signing_secret:
        # If no signing secret is configured, refuse all webhook traffic.
        # Safer than open routes.
        return False
    if not timestamp or not signature:
        return False
    try:
        # Reject replays older than 5 minutes per Slack guidance.
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False
    base = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)
