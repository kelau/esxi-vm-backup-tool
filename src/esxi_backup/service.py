from __future__ import annotations

import json
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
                    total_expected = sum(item.size for item in exports) or 1
                    transferred = 0
                    for item in exports:
                        self.repository.update_progress(
                            backup_id,
                            progress=min(95, int(transferred * 95 / total_expected)),
                            phase="exporting",
                            current_file=item.name,
                        )
                        last_percent = -1
                        current_file = item.name

                        def report(byte_count: int, current_file: str = current_file) -> None:
                            nonlocal transferred, last_percent
                            transferred += byte_count
                            percent = min(95, int(transferred * 95 / total_expected))
                            lease_percent = min(99, int(transferred * 100 / total_expected))
                            lease.HttpNfcLeaseProgress(lease_percent)
                            if percent != last_percent:
                                self.repository.update_progress(
                                    backup_id, progress=percent, phase="exporting",
                                    current_file=current_file,
                                )
                                last_percent = percent

                        with client.open_export(item.url) as stream:
                            chunks, file_logical, file_stored = self.repository.store_stream(
                                stream, on_bytes=report
                            )
                        files.append({"name": item.name, "size": file_logical, "chunks": chunks})
                        logical += file_logical
                        stored += file_stored
                self.repository.update_progress(
                    backup_id, progress=97, phase="writing manifest"
                )
                self.repository.write_manifest(backup_id, {
                    "format": 1, "backup_id": backup_id, "vm_id": vm._moId,
                    "vm_name": vm.name, "files": files,
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
        for file in manifest["files"]:
            safe_name = Path(file["name"]).name
            target = destination / safe_name
            with target.open("wb") as output:
                self.repository.restore_stream(file["chunks"], output)
            outputs.append(target)
        return outputs
