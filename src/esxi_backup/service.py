from __future__ import annotations

import io
import json
import tarfile
import uuid
from pathlib import Path

from .esxi import EsxiClient
from .models import AppConfig, BackupRecord, BackupStatus, VMInfo
from .repository import BackupRepository


class BackupService:
    def __init__(self, config: AppConfig, client_factory=EsxiClient):
        self.config = config
        self.client_factory = client_factory
        self.repository = BackupRepository(
            Path(config.repository), config.chunk_size_mib * 1024 * 1024, config.compression_level
        )

    def list_vms(self) -> list[VMInfo]:
        with self.client_factory(self.config.server) as client:
            return client.list_vms()

    def backup(self, identity: str) -> BackupRecord:
        backup_id = uuid.uuid4().hex
        with self.client_factory(self.config.server) as client:
            vm = client.find_vm(identity)
            record = BackupRecord(id=backup_id, vm_id=vm._moId, vm_name=vm.name,
                                  status=BackupStatus.RUNNING)
            self.repository.create(record)
            snapshot = None
            successful = False
            logical = stored = 0
            try:
                self.repository.update_progress(
                    backup_id, progress=1, phase="creating snapshot"
                )
                snapshot = client.create_snapshot(
                    vm, f"esxi-backup-{backup_id[:8]}", self.config.quiesce
                )
                files = []
                with client.export(vm) as (lease, exports):
                    ovf_descriptor = client.create_ovf_descriptor(vm, exports)
                    export_count = max(1, len(exports))
                    total_transferred = [0]
                    for file_index, item in enumerate(exports):
                        with client.open_export(item.url) as stream:
                            header_size = int(stream.headers.get("Content-Length", 0)) \
                                if hasattr(stream, "headers") else 0
                            current_size = item.size or header_size
                            state = {"file_bytes": 0, "last_percent": -1}

                            def report(
                                byte_count: int, *, state=state, file_index=file_index,
                                current_size=current_size, current_file=item.name,
                            ) -> None:
                                state["file_bytes"] += byte_count
                                total_transferred[0] += byte_count
                                fraction = (
                                    min(1.0, state["file_bytes"] / current_size)
                                    if current_size else 0.0
                                )
                                percent = min(
                                    95, max(2, int((file_index + fraction) * 95 / export_count))
                                )
                                lease.HttpNfcLeaseProgress(percent)
                                if percent != state["last_percent"] or current_size == 0:
                                    self.repository.update_progress(
                                        backup_id, progress=percent, phase="exporting",
                                        current_file=current_file,
                                        logical_bytes=total_transferred[0],
                                    )
                                    state["last_percent"] = percent

                            chunks, file_logical, file_stored = self.repository.store_stream(
                                stream, on_bytes=report
                            )
                        files.append({"name": item.name, "size": file_logical, "chunks": chunks})
                        files[-1]["device_id"] = item.device_id
                        logical += file_logical
                        stored += file_stored
                self.repository.update_progress(
                    backup_id, progress=97, phase="writing manifest"
                )
                self.repository.write_manifest(backup_id, {
                    "format": 1, "backup_id": backup_id, "vm_id": vm._moId,
                    "vm_name": vm.name, "ovf_descriptor": ovf_descriptor, "files": files,
                })
                successful = True
            except Exception as exc:
                self.repository.fail(backup_id, str(exc))
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
                        if successful:
                            self.repository.fail(
                                backup_id, f"Snapshot cleanup failed: {cleanup_error}"
                            )
                        raise
            if successful:
                self.repository.finish(backup_id, logical=logical, stored=stored)
        return self.repository.list(record.vm_id)[0]

    def restore(self, backup_id: str, destination: Path) -> list[Path]:
        manifest_path = self.repository.manifests / f"{backup_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        destination.mkdir(parents=True, exist_ok=True)
        outputs = []
        for index, file in enumerate(manifest["files"], start=1):
            raw_name = str(file["name"]).lower()
            if "nvram" in raw_name:
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
        self, backup_id: str, name: str, datastore: str | None = None
    ) -> None:
        manifest_path = self.repository.manifests / f"{backup_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        descriptor = manifest.get("ovf_descriptor")
        if not descriptor:
            raise ValueError(
                "This recovery point predates OVF capture; use offline restore and attach its disk."
            )
        with self.client_factory(self.config.server) as client:
            client.import_ovf(
                descriptor, manifest["files"], name,
                self.repository.iter_chunks, datastore,
            )

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
                for file in manifest["files"]:
                    info = tarfile.TarInfo(str(file["name"]))
                    info.size = int(file["size"])
                    info.mtime = 0
                    with self.repository.open_chunk_stream(file["chunks"]) as stream:
                        archive.addfile(info, stream)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination
