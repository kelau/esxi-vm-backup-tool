from __future__ import annotations

import io
import json
import tarfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

from .esxi import EsxiClient
from .models import AppConfig, BackupRecord, BackupStatus, VMInfo
from .portainer import PortainerClient
from .repository import BackupRepository


class BackupCancelled(Exception):
    pass


class BackupService:
    def __init__(self, config: AppConfig, client_factory=EsxiClient):
        self.config = config
        self.client_factory = client_factory
        self.repository = BackupRepository(
            Path(config.repository), config.chunk_size_mib * 1024 * 1024,
            config.compression_level,
            Path(config.secondary_repository) if config.secondary_repository else None,
        )
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_vm_ids: set[str] = set()
        self._cancel_lock = threading.Lock()
        self.update_pending = False

    def prepare_update(self) -> bool:
        """Atomically drain backup admission before the installer restarts us."""
        with self._cancel_lock:
            self.update_pending = True
            return not self._active_vm_ids

    def cancel_update(self) -> None:
        with self._cancel_lock:
            self.update_pending = False

    def list_containers(self) -> list[dict]:
        if not self.config.portainer:
            return []
        output = []
        with PortainerClient(self.config.portainer) as client:
            for endpoint in client.endpoints():
                for container in client.containers(endpoint["id"]):
                    output.append({**container, "endpoint_id": endpoint["id"],
                                   "endpoint_name": endpoint["name"]})
        return output

    def backup_container(self, endpoint_id: int, container_id: str) -> BackupRecord:
        if not self.config.portainer:
            raise RuntimeError("Portainer is not configured.")
        identity = f"container:{endpoint_id}:{container_id}"
        backup_id = uuid.uuid4().hex
        with PortainerClient(self.config.portainer) as client:
            details = client.inspect_container(endpoint_id, container_id)
            name = str(details.get("Name") or container_id[:12]).lstrip("/")
            cancel_event = self._begin_backup(backup_id, identity, name)
            record = BackupRecord(
                id=backup_id, vm_id=identity, vm_name=name, status=BackupStatus.RUNNING,
                phase="inspecting container",
            )
            self.repository.create(record)
            paused = False
            logical = stored = 0
            files = []
            try:
                running = bool(details.get("State", {}).get("Running"))
                if running and self.config.portainer.pause_during_backup:
                    self.repository.update_progress(
                        backup_id, progress=5, phase="pausing container"
                    )
                    client.pause(endpoint_id, container_id)
                    paused = True
                mounts = [
                    mount for mount in details.get("Mounts", [])
                    if mount.get("Type") == "volume" or (
                        mount.get("Type") == "bind"
                        and self.config.portainer.include_bind_mounts
                    )
                ]
                local_mounts = []
                skipped_mounts = []
                for mount in mounts:
                    if mount.get("Type") == "volume":
                        volume = client.inspect_volume(endpoint_id, mount["Name"])
                        if client.is_network_volume(volume):
                            skipped_mounts.append({"mount": mount, "reason": "network volume"})
                            continue
                    local_mounts.append(mount)
                mounts = local_mounts
                for index, mount in enumerate(mounts, start=1):
                    if cancel_event.is_set():
                        raise BackupCancelled("Backup cancelled by user")
                    label = mount.get("Name") or mount.get("Source") or f"mount-{index}"
                    self.repository.update_progress(
                        backup_id,
                        progress=10 + int(80 * (index - 1) / max(1, len(mounts))),
                        phase="archiving container storage (size unavailable)",
                        current_file=str(label),
                    )
                    transferred = 0
                    transfer_started = time.monotonic()
                    last_report = 0.0

                    def report_transfer(
                        count: int, force: bool = False, logical=logical,
                        index=index, label=label, transfer_started=transfer_started,
                    ) -> None:
                        nonlocal transferred, last_report
                        if cancel_event.is_set():
                            raise BackupCancelled("Backup cancelled by user")
                        transferred += count
                        now = time.monotonic()
                        if force or now - last_report >= 1:
                            self.repository.update_progress(
                                backup_id, logical_bytes=logical + transferred,
                                progress=10 + int(80 * (index - 1) / max(1, len(mounts))),
                                phase="archiving container storage (size unavailable)",
                                current_file=str(label),
                                throughput_mib_s=transferred / 1048576
                                / max(now - transfer_started, 0.001),
                            )
                            last_report = now

                    with client.archive(endpoint_id, container_id, mount["Destination"]) as stream:
                        chunks, file_logical, file_stored = self.repository.store_stream(
                            stream, workers=self.config.pipeline_workers,
                            on_read=report_transfer,
                        )
                    report_transfer(0, force=True)
                    logical += file_logical
                    stored += file_stored
                    files.append({
                        "name": f"{label}.tar", "size": file_logical, "chunks": chunks,
                        "mount": mount,
                    })
                self.repository.write_manifest(backup_id, {
                    "format": 1, "kind": "docker-container", "backup_id": backup_id,
                    "vm_id": identity, "vm_name": name, "endpoint_id": endpoint_id,
                    "container": details, "files": files, "skipped_mounts": skipped_mounts,
                })
                referenced = {
                    chunk["sha256"]: int(chunk["stored_size"])
                    for file in files for chunk in file["chunks"]
                }
                self.repository.finish(
                    backup_id, logical=logical, stored=stored,
                    repository_bytes=sum(referenced.values()), virtual=logical,
                )
            except BackupCancelled:
                self.repository.cancel(backup_id)
            except Exception as exc:
                self.repository.fail(backup_id, str(exc))
                raise
            finally:
                if paused:
                    client.unpause(endpoint_id, container_id)
                self._end_backup(backup_id, identity)
        self.repository.sync_mirror_background()
        return self.repository.list(identity)[0]

    def _begin_backup(self, backup_id: str, vm_id: str, vm_name: str) -> threading.Event:
        with self._cancel_lock:
            if self.update_pending:
                raise RuntimeError("An application update is pending. Retry after the update.")
            if vm_id in self._active_vm_ids:
                raise RuntimeError(f"A backup is already running for {vm_name}.")
            if len(self._active_vm_ids) >= self.config.max_concurrent_backups:
                raise RuntimeError(
                    "Backup concurrency limit reached "
                    f"({self.config.max_concurrent_backups}). Wait for an active job to finish."
                )
            event = threading.Event()
            self._active_vm_ids.add(vm_id)
            self._cancel_events[backup_id] = event
            return event

    def backup_capacity_error(self, vm_id: str | None = None) -> str | None:
        """Return an actionable admission error without allocating a transfer pipeline."""
        with self._cancel_lock:
            if self.update_pending:
                return "An application update is pending. Retry after the update."
            if vm_id is not None and vm_id in self._active_vm_ids:
                return "A backup is already running for this VM."
            if len(self._active_vm_ids) >= self.config.max_concurrent_backups:
                return (
                    "Backup concurrency limit reached "
                    f"({self.config.max_concurrent_backups}). Wait for an active job to finish."
                )
        return None

    def _end_backup(self, backup_id: str, vm_id: str) -> None:
        with self._cancel_lock:
            self._cancel_events.pop(backup_id, None)
            self._active_vm_ids.discard(vm_id)

    def cancel_backup(self, backup_id: str) -> bool:
        with self._cancel_lock:
            event = self._cancel_events.get(backup_id)
            if event is None:
                return False
            event.set()
            return True

    def list_vms(self) -> list[VMInfo]:
        with self.client_factory(self.config.server) as client:
            return client.list_vms()

    def refresh_storage_inventory(self) -> dict:
        with self.client_factory(self.config.server) as client:
            inventory = client.storage_inventory()
        return self.repository.save_storage_snapshot(inventory)

    def vm_details(self, identity: str, repository_only: bool = False) -> dict:
        backup_records = self.repository.list(identity)
        try:
            if repository_only:
                raise LookupError(identity)
            with self.client_factory(self.config.server) as client:
                details = client.get_vm_details(identity)
            details["inventory_state"] = "live"
        except LookupError:
            if not backup_records:
                raise
            newest = backup_records[0]
            details = {
                "id": identity, "name": newest.vm_name, "power_state": "unavailable",
                "guest_os": None, "guest_hostname": None, "ip_address": None,
                "tools_status": "unavailable", "cpu": 0, "memory_mib": 0,
                "firmware": None, "uuid": None, "datastores": [], "networks": [],
                "committed_bytes": 0, "uncommitted_bytes": newest.virtual_bytes,
                "disks": [], "inventory_state": "backup_only",
            }
        details["backups"] = [item.model_dump(mode="json") for item in backup_records]
        schedule = self.repository.get_schedule(identity)
        details["schedule"] = schedule.model_dump(mode="json") if schedule else None
        ova_exports = {item.backup_id: item for item in self.repository.list_ova_exports()}
        for backup in details["backups"]:
            export = ova_exports.get(backup["id"])
            backup["ova"] = export.model_dump(mode="json") if export else None
        return details

    def backup(self, identity: str, quiesce: bool | None = None) -> BackupRecord:
        if identity in self.config.excluded_vm_ids:
            raise RuntimeError("VM is excluded from backups")
        if error := self.repository.availability_error():
            raise RuntimeError(error)
        backup_id = uuid.uuid4().hex
        with self.client_factory(self.config.server) as client:
            vm = client.find_vm(identity)
            cancel_event = self._begin_backup(backup_id, vm._moId, vm.name)
            record = BackupRecord(id=backup_id, vm_id=vm._moId, vm_name=vm.name,
                                  status=BackupStatus.RUNNING)
            try:
                self.repository.create(record)
            except Exception:
                self._end_backup(backup_id, vm._moId)
                raise
            snapshot = None
            successful = False
            logical = stored = 0
            hardware = getattr(getattr(vm, "config", None), "hardware", None)
            virtual = sum(
                int(getattr(device, "capacityInBytes", 0))
                for device in (getattr(hardware, "device", None) or [])
            )
            try:
                power_state = str(getattr(getattr(vm, "runtime", None), "powerState", ""))
                if bool(getattr(getattr(vm, "runtime", None), "consolidationNeeded", False)):
                    raise RuntimeError(
                        "ESXi reports that this VM needs disk consolidation. Consolidate its "
                        "disks before starting another backup."
                    )
                export_source = vm
                if power_state != "poweredOff":
                    self.repository.update_progress(
                        backup_id, progress=1, phase="creating snapshot",
                        expected_bytes=virtual,
                    )
                    snapshot = client.create_snapshot(
                        vm, f"esxi-backup-{backup_id[:8]}",
                        self.config.quiesce if quiesce is None else quiesce,
                    )
                    export_source = snapshot
                files = []
                clone_started = time.monotonic()
                def report_preparation(done: int, total: int, current: str) -> None:
                    if cancel_event.is_set():
                        raise BackupCancelled("Backup cancelled by user")
                    percent = min(24, max(2, int(done * 24 / total))) if total else 2
                    elapsed = max(time.monotonic() - clone_started, 0.001)
                    self.repository.update_progress(
                        backup_id, progress=percent, phase="preparing hot clone",
                        current_file=current, logical_bytes=done,
                        expected_bytes=total or virtual,
                        throughput_mib_s=done / 1048576 / elapsed,
                    )

                export_context = (
                    client.export_hot(export_source, backup_id, report_preparation)
                    if snapshot is not None else client.export(export_source)
                )
                with export_context as (lease, exports):
                    ovf_descriptor = (
                        client.create_ovf_descriptor(vm, exports)
                        if lease is not None else None
                    )
                    export_count = max(1, len(exports))
                    total_transferred = 0
                    file_weights = [max(1, int(item.size or 0)) for item in exports]
                    active_files: set[str] = set()
                    progress_lock = threading.Lock()
                    transfer_started = time.monotonic()
                    # SSH clone sizes are final once vmkfstools completes, so they are the
                    # honest transfer total. NFC sizes may be missing or discovered late; use
                    # stable VM capacity there to keep lease progress monotonic.
                    expected_total = (
                        sum(file_weights) if lease is None else virtual or sum(file_weights)
                    )
                    last_reported_percent = 0
                    last_ui_update = 0.0

                    def download(file_index, item):
                        nonlocal total_transferred, last_reported_percent, last_ui_update
                        if cancel_event.is_set():
                            raise BackupCancelled("Backup cancelled by user")
                        with client.open_export(item.url) as stream:
                            header_size = int(stream.headers.get("Content-Length", 0)) \
                                if hasattr(stream, "headers") else 0
                            # The lease's device capacity can differ from the actual sparse NFC
                            # stream length. Prefer the HTTP byte count for honest progress.
                            current_size = header_size or item.size
                            with progress_lock:
                                active_files.add(item.name)

                            def report(byte_count: int) -> None:
                                nonlocal total_transferred, last_reported_percent, last_ui_update
                                if cancel_event.is_set():
                                    raise BackupCancelled("Backup cancelled by user")
                                with progress_lock:
                                    total_transferred += byte_count
                                    weighted_progress = (
                                        min(1.0, total_transferred / expected_total)
                                        if expected_total else 0
                                    )
                                    start = 25 if lease is None else 2
                                    calculated_percent = min(
                                        95,
                                        max(
                                            start,
                                            int(start + weighted_progress * (95 - start)),
                                        ),
                                    )
                                    percent = max(last_reported_percent, calculated_percent)
                                    elapsed = max(time.monotonic() - transfer_started, 0.001)
                                    throughput = total_transferred / 1048576 / elapsed
                                    if lease is not None and percent > last_reported_percent:
                                        lease.HttpNfcLeaseProgress(percent)
                                    now = time.monotonic()
                                    if percent > last_reported_percent or now-last_ui_update >= 1:
                                        phase = "exporting" if current_size or virtual else \
                                            "exporting (size unavailable)"
                                        current = ", ".join(sorted(active_files))
                                        if len(active_files) > 2:
                                            current = f"{len(active_files)} files"
                                        self.repository.update_progress(
                                            backup_id, progress=percent, phase=phase,
                                            current_file=current,
                                            logical_bytes=total_transferred,
                                            throughput_mib_s=throughput,
                                            expected_bytes=expected_total or None,
                                        )
                                        last_ui_update = now
                                    last_reported_percent = percent

                            chunks, file_logical, file_stored = self.repository.store_stream(
                                stream, on_read=report, workers=self.config.pipeline_workers
                            )
                        with progress_lock:
                            active_files.discard(item.name)
                        return {
                            "name": item.name, "size": file_logical, "chunks": chunks,
                            "device_id": item.device_id,
                        }, file_logical, file_stored

                    files = [None] * len(exports)
                    worker_count = min(self.config.parallel_disks, export_count)
                    with ThreadPoolExecutor(max_workers=worker_count) as executor:
                        futures = {
                            executor.submit(download, index, item): index
                            for index, item in enumerate(exports)
                        }
                        for future in as_completed(futures):
                            index = futures[future]
                            file_result, file_logical, file_stored = future.result()
                            files[index] = file_result
                            logical += file_logical
                            stored += file_stored
                if cancel_event.is_set():
                    raise BackupCancelled("Backup cancelled by user")
                self.repository.update_progress(
                    backup_id, progress=97, phase="writing manifest"
                )
                self.repository.write_manifest(backup_id, {
                    "format": 1, "backup_id": backup_id, "vm_id": vm._moId,
                    "vm_name": vm.name, "ovf_descriptor": ovf_descriptor, "files": files,
                    "transport": "nfc" if ovf_descriptor else "ssh-2gbsparse",
                })
                successful = True
            except BackupCancelled:
                self.repository.cancel(backup_id)
            except Exception as exc:
                self.repository.fail(backup_id, str(exc))
                self._end_backup(backup_id, vm._moId)
                raise
            finally:
                if snapshot is not None:
                    if successful:
                        self.repository.update_progress(
                            backup_id, progress=99, phase="removing snapshot"
                        )
                    try:
                        client.remove_snapshot(snapshot)
                    except Exception as cleanup_error:
                        self._end_backup(backup_id, vm._moId)
                        if successful:
                            self.repository.fail(
                                backup_id, f"Snapshot cleanup failed: {cleanup_error}"
                            )
                        raise
            if successful:
                referenced = {
                    chunk["sha256"]: int(chunk["stored_size"])
                    for file in files
                    for chunk in file["chunks"]
                }
                self.repository.finish(
                    backup_id, logical=logical, stored=stored,
                    repository_bytes=sum(referenced.values()), virtual=virtual,
                )
                self.repository.sync_mirror_background()
            self._end_backup(backup_id, vm._moId)
        return self.repository.list(record.vm_id)[0]

    def restore(self, backup_id: str, destination: Path) -> list[Path]:
        manifest_path = self.repository.manifests / f"{backup_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        destination.mkdir(parents=True, exist_ok=True)
        outputs = []
        preserve_names = manifest.get("transport") == "ssh-2gbsparse"
        for index, file in enumerate(manifest["files"], start=1):
            raw_name = str(file["name"]).lower()
            if preserve_names:
                safe_name = PurePosixPath(str(file["name"])).name
            elif "nvram" in raw_name:
                safe_name = "vm.nvram"
            elif any(kind in raw_name for kind in ("scsi", "sata", "ide", "vmdk")):
                safe_name = f"disk-{index:02d}.vmdk"
            else:
                safe_name = f"artifact-{index:02d}.bin"
            target = destination / safe_name
            with target.open("wb") as output:
                self.repository.restore_stream(file["chunks"], output)
            outputs.append(target)
        return outputs

    def restore_to_esxi(
        self, backup_id: str, name: str, datastore: str | None = None,
        on_progress=None,
    ) -> None:
        manifest_path = self.repository.manifests / f"{backup_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        descriptor = manifest.get("ovf_descriptor")
        if not descriptor:
            raise ValueError(
                "This recovery point has no OVF export. Reconstruct it with offline restore; "
                "SSH hot backups contain split sparse VMDK files that can be converted with "
                "vmkfstools and attached to a replacement VM."
            )
        with self.client_factory(self.config.server) as client:
            client.import_ovf(
                descriptor, manifest["files"], name,
                self.repository.iter_chunks, datastore, on_progress,
            )

    def restore_to_esxi_for_web(
        self, backup_id: str, name: str, datastore: str | None = None
    ) -> None:
        self.repository.start_restore(backup_id, name)
        try:
            self.restore_to_esxi(
                backup_id, name, datastore,
                lambda progress: self.repository.update_restore(backup_id, progress),
            )
            self.repository.finish_restore(backup_id)
        except Exception as exc:
            self.repository.fail_restore(backup_id, str(exc))
            raise

    def export_ova(self, backup_id: str, destination: Path) -> Path:
        manifest_path = self.repository.manifests / f"{backup_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        descriptor = manifest.get("ovf_descriptor")
        if not descriptor:
            raise ValueError(
                "This recovery point predates OVF capture and cannot be packaged as an OVA."
            )
        destination = destination.with_suffix(".ova")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".ova.partial")
        try:
            # OVF Tool rejects PAX extended headers; GNU tar supports VMDKs over 8 GiB.
            with tarfile.open(temporary, mode="w", format=tarfile.GNU_FORMAT) as archive:
                descriptor_bytes = descriptor.encode("utf-8")
                ovf_info = tarfile.TarInfo(f"{manifest['vm_name']}.ovf")
                ovf_info.size = len(descriptor_bytes)
                ovf_info.mtime = 0
                archive.addfile(ovf_info, io.BytesIO(descriptor_bytes))
                total = sum(int(file["size"]) for file in manifest["files"]) or 1
                written = 0
                last_progress = 0.0
                last_update = 0.0

                def report_progress(count: int, *, force: bool = False) -> None:
                    nonlocal written, last_progress, last_update
                    written += count
                    progress = min(99.9, round(written * 100 / total, 1))
                    now = time.monotonic()
                    if force or (progress > last_progress and now - last_update >= 0.5):
                        self.repository.update_ova_export(backup_id, progress)
                        last_progress = progress
                        last_update = now

                for file in manifest["files"]:
                    member_name = str(file["name"])
                    if PurePosixPath(member_name).name != member_name:
                        raise ValueError(f"Unsafe OVA member name: {member_name}")
                    info = tarfile.TarInfo(member_name)
                    info.size = int(file["size"])
                    info.mtime = 0
                    with self.repository.open_chunk_stream(
                        file["chunks"], on_read=report_progress
                    ) as stream:
                        archive.addfile(info, stream)
                    report_progress(0, force=True)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def export_ova_for_web(self, backup_id: str) -> None:
        self.repository.start_ova_export(backup_id)
        destination = self.repository.root / "exports" / f"{backup_id}.ova"
        try:
            output = self.export_ova(backup_id, destination)
            self.repository.finish_ova_export(backup_id, output)
            self.repository.sync_mirror_background()
        except Exception as exc:
            self.repository.fail_ova_export(backup_id, str(exc))
            raise

    def supports_ova(self, backup_id: str) -> bool:
        path = self.repository.manifests / f"{backup_id}.json"
        if not path.exists():
            return False
        return bool(json.loads(path.read_text(encoding="utf-8")).get("ovf_descriptor"))
