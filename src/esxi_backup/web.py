from __future__ import annotations

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import load_config, resolve_config_path, save_config
from .models import AppConfig, RetentionConfig, ServerConfig
from .service import BackupService

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def create_app(config_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="ESXi VM Backup Tool", version="0.1.0")
    app.state.service = BackupService(load_config(config_path))
    app.state.config_path = resolve_config_path(config_path)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        try:
            vms = app.state.service.list_vms()
            connection_error = None
        except Exception as exc:
            vms, connection_error = [], str(exc)
        backups = app.state.service.repository.list()
        latest = {}
        for backup in backups:
            latest.setdefault(backup.vm_id, backup)
        return templates.TemplateResponse(request, "dashboard.html", {
            "vms": vms, "backups": backups[:25], "latest": latest,
            "connection_error": connection_error,
            "config": app.state.service.config,
        })

    @app.get("/api/v1/vms")
    def api_vms():
        return app.state.service.list_vms()

    @app.get("/api/v1/backups")
    def api_backups():
        return app.state.service.repository.list()

    @app.get("/api/v1/config")
    def api_config():
        config = app.state.service.config.model_dump(mode="json", exclude={"server": {"password"}})
        return config

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: bool = False):
        return templates.TemplateResponse(request, "settings.html", {
            "config": app.state.service.config,
            "saved": saved,
            "error": None,
        })

    @app.post("/settings", response_class=HTMLResponse)
    def update_settings(
        request: Request,
        host: str = Form(),
        username: str = Form(),
        password: str = Form(default=""),
        port: int = Form(),
        verify_ssl: bool = Form(default=False),
        repository: str = Form(),
        chunk_size_mib: int = Form(),
        compression_level: int = Form(),
        quiesce: bool = Form(default=False),
        keep_last: int = Form(),
        keep_daily: int = Form(),
        keep_weekly: int = Form(),
        keep_monthly: int = Form(),
    ):
        current = app.state.service.config
        try:
            updated = AppConfig(
                server=ServerConfig(
                    host=host.strip(), username=username.strip(),
                    password=password or current.server.password.get_secret_value(),
                    port=port, verify_ssl=verify_ssl,
                ),
                repository=repository.strip(), chunk_size_mib=chunk_size_mib,
                compression_level=compression_level, quiesce=quiesce,
                retention=RetentionConfig(
                    keep_last=keep_last, keep_daily=keep_daily,
                    keep_weekly=keep_weekly, keep_monthly=keep_monthly,
                ),
            )
            save_config(updated, app.state.config_path)
            app.state.service = BackupService(updated)
        except Exception as exc:
            return templates.TemplateResponse(request, "settings.html", {
                "config": current, "saved": False, "error": str(exc),
            }, status_code=422)
        return RedirectResponse("/settings?saved=true", status_code=303)

    @app.post("/api/v1/vms/{vm_id}/backups", status_code=202)
    def api_backup(vm_id: str, tasks: BackgroundTasks):
        tasks.add_task(app.state.service.backup, vm_id)
        return {"accepted": True, "vm_id": vm_id}

    @app.post("/vms/{vm_id}/backups")
    def html_backup(vm_id: str, tasks: BackgroundTasks):
        tasks.add_task(app.state.service.backup, vm_id)
        return RedirectResponse("/", status_code=303)

    @app.exception_handler(LookupError)
    def not_found(_request: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app
