from __future__ import annotations

import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
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
templates.env.globals["app_version"] = __version__
optional_vm_ids = Form(default=None)


def create_app(config_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        instance.state.scheduler.start()
        yield
        instance.state.scheduler.shutdown()

    app = FastAPI(title="ESXi VM Backup Tool", version=__version__, lifespan=lifespan)
    app.state.service = BackupService(load_config(config_path))
    app.state.config_path = resolve_config_path(config_path)
    app.state.scheduler = BackupScheduler(app.state.service)
    app.state.storage_refresh = {
        "status": "idle", "started_at": None, "finished_at": None, "error": None,
    }
    app.state.storage_refresh_lock = threading.Lock()

    def run_storage_refresh():
        with app.state.storage_refresh_lock:
            app.state.storage_refresh.update(
                status="running", started_at=datetime.now(UTC), finished_at=None, error=None
            )
        try:
            app.state.service.refresh_storage_inventory()
        except Exception as exc:
            with app.state.storage_refresh_lock:
                app.state.storage_refresh.update(
                    status="failed", finished_at=datetime.now(UTC), error=str(exc)
                )
        else:
            with app.state.storage_refresh_lock:
                app.state.storage_refresh.update(
                    status="success", finished_at=datetime.now(UTC), error=None
                )

    def queue_storage_refresh(tasks: BackgroundTasks) -> bool:
        with app.state.storage_refresh_lock:
            if app.state.storage_refresh["status"] in {"queued", "running"}:
                return False
            app.state.storage_refresh.update(
                status="queued", started_at=datetime.now(UTC), finished_at=None, error=None
            )
        tasks.add_task(run_storage_refresh)
        return True

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        try:
            vms = app.state.service.list_vms()
            connection_error = None
        except Exception as exc:
            vms, connection_error = [], str(exc)
        repository_error = app.state.service.repository.availability_error()
        if repository_error:
            vm_states = {
                vm.id: ("live" if vm.connection_state == "connected" else "inaccessible")
                for vm in vms
            }
            return templates.TemplateResponse(request, "dashboard.html", {
                "vms": vms, "backups": [], "latest": {},
                "connection_error": connection_error, "repository_error": repository_error,
                "repository_available": False, "config": app.state.service.config,
                "schedules": {}, "schedule_info": {}, "ova_exports": {},
                "ova_capable": set(), "restores": {}, "vm_states": vm_states,
                "latest_recovery": {}, "repository_vm_ids": set(),
                "repository_stats": {"total_bytes": 0, "chunk_bytes": 0,
                                     "ova_bytes": 0, "recovery_points": 0},
            })
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
        schedule_info = {}
        for policy in app.state.scheduler.policies():
            for vm_id in policy.vm_ids:
                state = schedule_info.setdefault(vm_id, {"active": [], "disabled": []})
                state["disabled" if policy.frequency == "disabled" else "active"].append(
                    policy.name
                )
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
            "schedule_info": schedule_info,
            "ova_exports": ova_exports,
            "ova_capable": ova_capable,
            "restores": restores,
            "repository_stats": repository_stats,
            "vm_states": vm_states,
            "latest_recovery": latest_recovery,
            "repository_vm_ids": set(latest_job),
            "repository_error": None, "repository_available": True,
        })

    def repository_unavailable():
        error = app.state.service.repository.availability_error()
        return JSONResponse(status_code=503, content={"detail": error}) if error else None

    @app.get("/api/v1/vms")
    def api_vms():
        return app.state.service.list_vms()

    @app.get("/api/v1/vms/{vm_id}")
    def api_vm_details(vm_id: str, repository_only: bool = False):
        if response := repository_unavailable():
            return response
        return app.state.service.vm_details(vm_id, repository_only=repository_only)

    @app.get("/api/v1/backups")
    def api_backups():
        if response := repository_unavailable():
            return response
        return app.state.service.repository.list()

    @app.get("/api/v1/repository")
    def api_repository():
        if response := repository_unavailable():
            return response
        return app.state.service.repository.stats()

    @app.get("/api/v1/storage")
    def api_storage():
        if response := repository_unavailable():
            return response
        return app.state.service.repository.latest_storage_snapshot()

    @app.post("/api/v1/storage/refresh", status_code=202)
    def api_storage_refresh(tasks: BackgroundTasks):
        if response := repository_unavailable():
            return response
        queued = queue_storage_refresh(tasks)
        return {"accepted": queued, **app.state.storage_refresh}

    @app.get("/api/v1/storage/refresh-status")
    def api_storage_refresh_status():
        with app.state.storage_refresh_lock:
            return dict(app.state.storage_refresh)

    @app.get("/datastores", response_class=HTMLResponse)
    def datastores_page(
        request: Request, tasks: BackgroundTasks, refresh: bool = False,
    ):
        error = None
        try:
            snapshot = app.state.service.repository.latest_storage_snapshot()
            history = app.state.service.repository.storage_snapshots()
            if refresh:
                queue_storage_refresh(tasks)
        except Exception as exc:
            snapshot, history = None, []
            error = str(exc)
        return templates.TemplateResponse(request, "datastores.html", {
            "snapshot": snapshot, "error": error,
            "history": history, "refresh_state": dict(app.state.storage_refresh),
        })

    @app.get("/api/v1/ova-exports")
    def api_ova_exports():
        if response := repository_unavailable():
            return response
        return app.state.service.repository.list_ova_exports()

    @app.get("/api/v1/restores")
    def api_restores():
        if response := repository_unavailable():
            return response
        return app.state.service.repository.list_restores()

    @app.get("/api/v1/schedules")
    def api_schedules():
        if response := repository_unavailable():
            return response
        return app.state.scheduler.policies()

    def home_assistant_payload(include_vms: bool = True):
        """Build a stable, sensor-friendly view without leaking configuration secrets."""
        generated_at = datetime.now(UTC)
        connection_error = None
        try:
            live_vms = app.state.service.list_vms()
        except Exception as exc:
            live_vms, connection_error = [], str(exc)

        repository_error = app.state.service.repository.availability_error()
        backups = [] if repository_error else app.state.service.repository.list()
        stats = None if repository_error else app.state.service.repository.stats()
        latest_success = {}
        latest_job = {}
        for backup in backups:
            latest_job.setdefault(backup.vm_id, backup)
            if backup.status == "success":
                latest_success.setdefault(backup.vm_id, backup)

        policies_by_vm = {}
        for policy in app.state.scheduler.policies() if not repository_error else []:
            if policy.frequency == "disabled":
                continue
            for vm_id in policy.vm_ids:
                policies_by_vm.setdefault(vm_id, []).append(policy.name)

        vm_map = {vm.id: vm for vm in live_vms}
        for vm_id, backup in latest_job.items():
            if vm_id not in vm_map:
                vm_map[vm_id] = VMInfo(
                    id=vm_id, name=backup.vm_name, power_state="unavailable",
                    connection_state="backup_only", guest_os="Repository recovery point",
                    provisioned_bytes=backup.virtual_bytes,
                )

        vm_payload = []
        for vm in sorted(vm_map.values(), key=lambda item: item.name.casefold()):
            success = latest_success.get(vm.id)
            job = latest_job.get(vm.id)
            backup_time = (success.finished_at or success.started_at) if success else None
            if vm.connection_state == "backup_only":
                inventory_state = "backup_only"
            elif vm.connection_state == "connected":
                inventory_state = "live"
            else:
                inventory_state = "inaccessible"
            vm_payload.append({
                "id": vm.id,
                "name": vm.name,
                "inventory_state": inventory_state,
                "power_state": vm.power_state,
                "backup_state": (
                    "running" if job and job.status == "running"
                    else "protected" if success else "unprotected"
                ),
                "last_backup_at": backup_time,
                "last_backup_age_seconds": (
                    max(0, int((generated_at - backup_time).total_seconds()))
                    if backup_time else None
                ),
                "backup_size_bytes": success.repository_bytes if success else 0,
                "provisioned_bytes": vm.provisioned_bytes,
                "scheduled": vm.id in policies_by_vm,
                "schedules": policies_by_vm.get(vm.id, []),
            })

        errors = {
            key: value for key, value in {
                "esxi": connection_error, "repository": repository_error,
            }.items() if value
        }
        payload = {
            "status": "ok" if not errors else "degraded",
            "version": __version__,
            "generated_at": generated_at,
            "esxi_connected": connection_error is None,
            "repository_available": repository_error is None,
            "vm_count": len(vm_payload),
            "protected_vm_count": sum(vm["backup_state"] == "protected" for vm in vm_payload),
            "scheduled_vm_count": sum(vm["scheduled"] for vm in vm_payload),
            "active_backup_count": sum(item.status == "running" for item in backups),
            "failed_backup_count": sum(item.status == "failed" for item in backups),
            "repository": stats,
            "errors": errors,
        }
        if include_vms:
            payload["vms"] = vm_payload
        return payload

    @app.get("/api/v1/home-assistant")
    def api_home_assistant(include_vms: bool = True):
        return home_assistant_payload(include_vms)

    @app.get("/api/v1/home-assistant/vms/{vm_id}")
    def api_home_assistant_vm(vm_id: str):
        payload = home_assistant_payload()
        vm = next((item for item in payload["vms"] if item["id"] == vm_id), None)
        if vm is None:
            raise LookupError(f"VM not found: {vm_id}")
        return vm

    @app.post("/api/v1/home-assistant/vms/{vm_id}/backup", status_code=202)
    def api_home_assistant_backup(vm_id: str, tasks: BackgroundTasks):
        if response := repository_unavailable():
            return response
        tasks.add_task(app.state.service.backup, vm_id)
        return {"accepted": True, "vm_id": vm_id}

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request):
        try:
            vms = app.state.service.list_vms()
            if repository_error := app.state.service.repository.availability_error():
                schedules, error = [], repository_error
            else:
                schedules, error = app.state.scheduler.policies(), None
        except Exception as exc:
            vms, schedules, error = [], [], str(exc)
        return templates.TemplateResponse(request, "schedules.html", {
            "schedules": schedules, "vms": vms, "error": error,
        })

    @app.post("/schedules")
    def save_schedule_policy(
        name: str = Form(), vm_ids: list[str] | None = optional_vm_ids,
        schedule_id: str = Form(default=""), frequency: str = Form(default="daily"),
        hour: int = Form(default=2), minute: int = Form(default=0),
        weekday: int = Form(default=0), quiesce: bool = Form(default=False),
        build_ova: bool = Form(default=False),
    ):
        if response := repository_unavailable():
            return response
        schedule = SchedulePolicy(
            id=schedule_id or uuid.uuid4().hex, name=name.strip(), vm_ids=vm_ids or [],
            frequency=frequency, hour=hour, minute=minute, weekday=weekday,
            quiesce=quiesce, build_ova=build_ova,
        )
        app.state.scheduler.apply_policy(schedule)
        app.state.service.repository.sync_mirror_background()
        return RedirectResponse("/schedules", status_code=303)

    @app.post("/schedules/{schedule_id}/delete")
    def delete_schedule_policy(schedule_id: str):
        if response := repository_unavailable():
            return response
        app.state.scheduler.delete_policy(schedule_id)
        app.state.service.repository.sync_mirror_background()
        return RedirectResponse("/schedules", status_code=303)

    @app.put("/api/v1/vms/{vm_id}/schedule")
    def api_schedule(vm_id: str, schedule: BackupSchedule):
        if response := repository_unavailable():
            return response
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
        secondary_repository: str = Form(default=""),
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
                repository=repository.strip(),
                secondary_repository=secondary_repository.strip() or None,
                chunk_size_mib=chunk_size_mib,
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
            app.state.service.repository.sync_mirror_background()
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
        if response := repository_unavailable():
            return response
        if frequency not in {"disabled", "daily", "weekly"}:
            return JSONResponse(status_code=422, content={"detail": "Invalid frequency"})
        app.state.scheduler.apply(BackupSchedule(
            vm_id=vm_id, vm_name=vm_name, frequency=frequency,
            hour=hour, minute=minute, weekday=weekday,
        ))
        return RedirectResponse("/", status_code=303)

    @app.post("/api/v1/vms/{vm_id}/backups", status_code=202)
    def api_backup(vm_id: str, tasks: BackgroundTasks):
        if response := repository_unavailable():
            return response
        tasks.add_task(app.state.service.backup, vm_id)
        return {"accepted": True, "vm_id": vm_id}

    @app.post("/vms/{vm_id}/backups")
    def html_backup(vm_id: str, tasks: BackgroundTasks):
        if response := repository_unavailable():
            return response
        tasks.add_task(app.state.service.backup, vm_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/repository/vms/{vm_id}/delete")
    def delete_repository_vm(vm_id: str):
        try:
            result = app.state.service.repository.delete_vm(vm_id)
            app.state.service.repository.sync_mirror_background()
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
