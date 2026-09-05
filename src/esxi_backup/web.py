from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import load_config, resolve_config_path, save_config
from .models import (
    AppConfig,
    BackupSchedule,
    RetentionConfig,
    SchedulePolicy,
    ServerConfig,
    VMInfo,
)
from .scheduler import BackupScheduler
from .service import BackupService

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
optional_vm_ids = Form(default=None)


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
        latest_job = {}
        latest = {}
        for backup in backups:
            latest_job.setdefault(backup.vm_id, backup)
            if backup.status in {"running", "success"}:
                latest.setdefault(backup.vm_id, backup)
        live_vm_ids = {vm.id for vm in vms}
        vm_states = {
            vm.id: (
                "live" if vm.connection_state == "connected" else "inaccessible"
            )
            for vm in vms
        }
        for vm_id, backup in latest_job.items():
            if vm_id not in live_vm_ids:
                vms.append(VMInfo(
                    id=vm_id, name=backup.vm_name, power_state="unavailable",
                    guest_os="Repository recovery point",
                    provisioned_bytes=backup.virtual_bytes,
                ))
                vm_states[vm_id] = "backup_only"
        schedules = {item.vm_id: item for item in app.state.scheduler.schedules()}
        ova_exports = {
            item.backup_id: item for item in app.state.service.repository.list_ova_exports()
        }
        ova_capable = {
            backup.id for backup in backups if app.state.service.supports_ova(backup.id)
        }
        latest_recovery = {}
        for backup in backups:
            if backup.status == "success" and backup.id in ova_capable:
                latest_recovery.setdefault(backup.vm_id, backup)
        restores = {
            item.backup_id: item for item in app.state.service.repository.list_restores()
        }
        repository_stats = app.state.service.repository.stats()
        return templates.TemplateResponse(request, "dashboard.html", {
            "vms": vms, "backups": backups[:25], "latest": latest,
            "connection_error": connection_error,
            "config": app.state.service.config,
            "schedules": schedules,
            "ova_exports": ova_exports,
            "ova_capable": ova_capable,
            "restores": restores,
            "repository_stats": repository_stats,
            "vm_states": vm_states,
            "latest_recovery": latest_recovery,
            "repository_vm_ids": set(latest_job),
        })

    @app.get("/api/v1/vms")
    def api_vms():
        return app.state.service.list_vms()

    @app.get("/api/v1/vms/{vm_id}")
    def api_vm_details(vm_id: str, repository_only: bool = False):
        return app.state.service.vm_details(vm_id, repository_only=repository_only)

    @app.get("/api/v1/backups")
    def api_backups():
        return app.state.service.repository.list()

    @app.get("/api/v1/repository")
    def api_repository():
        return app.state.service.repository.stats()

    @app.get("/api/v1/ova-exports")
    def api_ova_exports():
        return app.state.service.repository.list_ova_exports()

    @app.get("/api/v1/restores")
    def api_restores():
        return app.state.service.repository.list_restores()

    @app.get("/api/v1/schedules")
    def api_schedules():
        return app.state.scheduler.policies()

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request):
        try:
            vms = app.state.service.list_vms()
            error = None
        except Exception as exc:
            vms, error = [], str(exc)
        return templates.TemplateResponse(request, "schedules.html", {
            "schedules": app.state.scheduler.policies(), "vms": vms, "error": error,
        })

    @app.post("/schedules")
    def save_schedule_policy(
        name: str = Form(), vm_ids: list[str] | None = optional_vm_ids,
        schedule_id: str = Form(default=""), frequency: str = Form(default="daily"),
        hour: int = Form(default=2), minute: int = Form(default=0),
        weekday: int = Form(default=0), quiesce: bool = Form(default=False),
        build_ova: bool = Form(default=False),
    ):
        schedule = SchedulePolicy(
            id=schedule_id or uuid.uuid4().hex, name=name.strip(), vm_ids=vm_ids or [],
            frequency=frequency, hour=hour, minute=minute, weekday=weekday,
            quiesce=quiesce, build_ova=build_ova,
        )
        app.state.scheduler.apply_policy(schedule)
        return RedirectResponse("/schedules", status_code=303)

    @app.post("/schedules/{schedule_id}/delete")
    def delete_schedule_policy(schedule_id: str):
        app.state.scheduler.delete_policy(schedule_id)
        return RedirectResponse("/schedules", status_code=303)

    @app.put("/api/v1/vms/{vm_id}/schedule")
    def api_schedule(vm_id: str, schedule: BackupSchedule):
        if schedule.vm_id != vm_id:
            return JSONResponse(status_code=400, content={"detail": "VM ID mismatch"})
        return app.state.scheduler.apply(schedule)

    @app.get("/api/v1/config")
    def api_config():
        config = app.state.service.config.model_dump(
            mode="json", exclude={"server": {"password", "ssh_password"}}
        )
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
        ssh_enabled: bool = Form(default=False),
        ssh_port: int = Form(default=22),
        ssh_username: str = Form(default=""),
        ssh_password: str = Form(default=""),
        ssh_verify_host_key: bool = Form(default=False),
        repository: str = Form(),
        chunk_size_mib: int = Form(),
        compression_level: int = Form(),
        pipeline_workers: int = Form(),
        parallel_disks: int = Form(),
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
                    ssh_enabled=ssh_enabled, ssh_port=ssh_port,
                    ssh_username=ssh_username.strip() or None,
                    ssh_password=(
                        ssh_password
                        or (current.server.ssh_password.get_secret_value()
                            if current.server.ssh_password else None)
                    ),
                    ssh_verify_host_key=ssh_verify_host_key,
                    ssh_host_key=current.server.ssh_host_key,
                ),
                repository=repository.strip(), chunk_size_mib=chunk_size_mib,
                compression_level=compression_level, pipeline_workers=pipeline_workers,
                parallel_disks=parallel_disks, quiesce=quiesce,
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

    @app.post("/settings/trust-ssh-host")
    def trust_ssh_host(request: Request):
        current = app.state.service.config
        try:
            with app.state.service.client_factory(current.server) as client:
                host_key, _fingerprint = client.inspect_ssh_host_key()
            updated = current.model_copy(deep=True)
            updated.server.ssh_host_key = host_key
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

    @app.post("/repository/vms/{vm_id}/delete")
    def delete_repository_vm(vm_id: str):
        try:
            result = app.state.service.repository.delete_vm(vm_id)
        except RuntimeError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        return JSONResponse(content=result)

    @app.post("/backups/{backup_id}/cancel")
    def cancel_backup(backup_id: str):
        if not app.state.service.cancel_backup(backup_id):
            return JSONResponse(
                status_code=409, content={"detail": "Backup is no longer active"}
            )
        return JSONResponse(status_code=202, content={"accepted": True})

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
        backup = next(
            (item for item in app.state.service.repository.list() if item.id == backup_id), None
        )
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", backup.vm_name).strip(". ") \
            if backup else backup_id
        return FileResponse(
            path, filename=f"{safe_name or backup_id}.ova",
            media_type="application/x-virtualization-ova",
        )

    @app.post("/backups/{backup_id}/restore-to-esxi")
    def restore_backup(
        backup_id: str,
        tasks: BackgroundTasks,
        name: str = Form(),
        datastore: str = Form(default=""),
    ):
        if not app.state.service.supports_ova(backup_id):
            return JSONResponse(
                status_code=409,
                content={"detail": "Recovery point has no OVF descriptor"},
            )
        clean_name = name.strip()
        if not clean_name:
            return JSONResponse(status_code=422, content={"detail": "VM name is required"})
        app.state.service.repository.start_restore(backup_id, clean_name)
        tasks.add_task(
            app.state.service.restore_to_esxi_for_web,
            backup_id, clean_name, datastore.strip() or None,
        )
        return JSONResponse(status_code=202, content={"accepted": True})

    @app.exception_handler(LookupError)
    def not_found(_request: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app
