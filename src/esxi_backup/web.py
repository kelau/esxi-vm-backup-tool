from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import load_config, resolve_config_path, save_config
from .models import AppConfig, BackupSchedule, RetentionConfig, ServerConfig
from .scheduler import BackupScheduler
from .service import BackupService

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def create_app(config_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        instance.state.scheduler.start()
        yield
        instance.state.scheduler.shutdown()

    app = FastAPI(title="ESXi VM Backup Tool", version="0.1.0", lifespan=lifespan)
    app.state.service = BackupService(load_config(config_path))
    app.state.config_path = resolve_config_path(config_path)
    app.state.scheduler = BackupScheduler(app.state.service)

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
        schedules = {item.vm_id: item for item in app.state.scheduler.schedules()}
        ova_exports = {
            item.backup_id: item for item in app.state.service.repository.list_ova_exports()
        }
        ova_capable = {
            backup.id for backup in backups if app.state.service.supports_ova(backup.id)
        }
        return templates.TemplateResponse(request, "dashboard.html", {
            "vms": vms, "backups": backups[:25], "latest": latest,
            "connection_error": connection_error,
            "config": app.state.service.config,
            "schedules": schedules,
            "ova_exports": ova_exports,
            "ova_capable": ova_capable,
        })

    @app.get("/api/v1/vms")
    def api_vms():
        return app.state.service.list_vms()

    @app.get("/api/v1/vms/{vm_id}")
    def api_vm_details(vm_id: str):
        return app.state.service.vm_details(vm_id)

    @app.get("/api/v1/backups")
    def api_backups():
        return app.state.service.repository.list()

    @app.get("/api/v1/ova-exports")
    def api_ova_exports():
        return app.state.service.repository.list_ova_exports()

    @app.get("/api/v1/schedules")
    def api_schedules():
        return app.state.scheduler.schedules()

    @app.put("/api/v1/vms/{vm_id}/schedule")
    def api_schedule(vm_id: str, schedule: BackupSchedule):
        if schedule.vm_id != vm_id:
            return JSONResponse(status_code=400, content={"detail": "VM ID mismatch"})
        return app.state.scheduler.apply(schedule)

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
            app.state.scheduler.shutdown()
            app.state.service = BackupService(updated)
            app.state.scheduler = BackupScheduler(app.state.service)
            app.state.scheduler.start()
        except Exception as exc:
            return templates.TemplateResponse(request, "settings.html", {
                "config": current, "saved": False, "error": str(exc),
            }, status_code=422)
        return RedirectResponse("/settings?saved=true", status_code=303)

    @app.post("/vms/{vm_id}/schedule")
    def update_schedule(
        vm_id: str,
        vm_name: str = Form(),
        frequency: str = Form(),
        hour: int = Form(),
        minute: int = Form(),
        weekday: int = Form(default=0),
    ):
        if frequency not in {"disabled", "daily", "weekly"}:
            return JSONResponse(status_code=422, content={"detail": "Invalid frequency"})
        app.state.scheduler.apply(BackupSchedule(
            vm_id=vm_id, vm_name=vm_name, frequency=frequency,
            hour=hour, minute=minute, weekday=weekday,
        ))
        return RedirectResponse("/", status_code=303)

    @app.post("/api/v1/vms/{vm_id}/backups", status_code=202)
    def api_backup(vm_id: str, tasks: BackgroundTasks):
        tasks.add_task(app.state.service.backup, vm_id)
        return {"accepted": True, "vm_id": vm_id}

    @app.post("/vms/{vm_id}/backups")
    def html_backup(vm_id: str, tasks: BackgroundTasks):
        tasks.add_task(app.state.service.backup, vm_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/backups/{backup_id}/ova")
    def build_ova(backup_id: str, tasks: BackgroundTasks):
        if not app.state.service.supports_ova(backup_id):
            return JSONResponse(
                status_code=409,
                content={"detail": "Recovery point has no OVF descriptor"},
            )
        app.state.service.repository.start_ova_export(backup_id)
        tasks.add_task(app.state.service.export_ova_for_web, backup_id)
        return RedirectResponse("/", status_code=303)

    @app.get("/backups/{backup_id}/ova")
    def download_ova(backup_id: str):
        export = app.state.service.repository.get_ova_export(backup_id)
        if not export or export.status != "success" or not export.path:
            return JSONResponse(status_code=404, content={"detail": "OVA not available"})
        path = Path(export.path).resolve()
        exports_root = (app.state.service.repository.root / "exports").resolve()
        if exports_root not in path.parents or not path.is_file():
            return JSONResponse(status_code=404, content={"detail": "OVA not available"})
        return FileResponse(path, filename=path.name, media_type="application/x-virtualization-ova")

    @app.exception_handler(LookupError)
    def not_found(_request: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app
