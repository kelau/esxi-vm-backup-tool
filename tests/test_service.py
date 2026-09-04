from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest

pytest.importorskip("zstandard")

from esxi_backup.models import AppConfig, ServerConfig, VMInfo
from esxi_backup.service import BackupService


class FakeLease:
    def __init__(self):
        self.progress = []

    def HttpNfcLeaseProgress(self, value):
        self.progress.append(value)


class FakeClient:
    removed = False

    def __init__(self, _config):
        self.vm = SimpleNamespace(_moId="vm-42", name="mail")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def list_vms(self):
        return [VMInfo(id="vm-42", name="mail", power_state="poweredOn")]

    def find_vm(self, identity):
        if identity not in ("vm-42", "mail"):
            raise LookupError(identity)
        return self.vm

    def create_snapshot(self, _vm, _name, _quiesce):
        return object()

    def remove_snapshot(self, _snapshot):
        FakeClient.removed = True

    @contextmanager
    def export(self, _vm):
        yield FakeLease(), [SimpleNamespace(name="disk.vmdk", url="memory://disk", size=12)]

    def open_export(self, _url):
        return BytesIO(b"virtual disk")


def config(tmp_path):
    return AppConfig(
        server=ServerConfig(host="host", username="user", password="secret"),
        repository=str(tmp_path), chunk_size_mib=1,
    )


def test_backup_happy_path_and_snapshot_cleanup(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    record = service.backup("mail")
    assert record.status == "success"
    assert record.progress == 100
    assert record.phase == "complete"
    assert record.logical_bytes == 12
    assert FakeClient.removed
    assert (tmp_path / "manifests" / f"{record.id}.json").exists()


def test_list_vms(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    assert service.list_vms()[0].name == "mail"


def test_restore_maps_esxi_device_keys_to_portable_names(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    disk_chunks, _, _ = service.repository.store_stream(BytesIO(b"disk-data"))
    nvram_chunks, _, _ = service.repository.store_stream(BytesIO(b"nvram-data"))
    service.repository.write_manifest("restore-me", {
        "format": 1, "backup_id": "restore-me", "vm_id": "vm-42", "vm_name": "mail",
        "files": [
            {"name": "/10/ParaVirtualSCSIController0:0", "chunks": disk_chunks},
            {"name": "/10/nvram", "chunks": nvram_chunks},
        ],
    })

    outputs = service.restore("restore-me", tmp_path / "output")

    assert [path.name for path in outputs] == ["disk-01.vmdk", "vm.nvram"]
    assert outputs[0].read_bytes() == b"disk-data"
