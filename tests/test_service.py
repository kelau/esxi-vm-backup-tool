import tarfile
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
    imported = None

    def __init__(self, _config):
        self.vm = SimpleNamespace(
            _moId="vm-42", name="mail",
            config=SimpleNamespace(hardware=SimpleNamespace(
                device=[SimpleNamespace(capacityInBytes=1024)]
            )),
        )

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
        yield FakeLease(), [SimpleNamespace(
            name="disk.vmdk", url="memory://disk", size=12, device_id="disk-1"
        )]

    def create_ovf_descriptor(self, _vm, _exports):
        return "<ovf>descriptor</ovf>"

    def open_export(self, _url):
        return BytesIO(b"virtual disk")

    def import_ovf(self, descriptor, files, name, chunk_reader, datastore, on_progress=None):
        FakeClient.imported = (descriptor, files, name, datastore)
        if on_progress:
            on_progress(75)


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
    assert record.virtual_bytes == 1024
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


def test_restore_to_esxi_uses_captured_ovf(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    service.repository.write_manifest("ovf-backup", {
        "format": 1, "backup_id": "ovf-backup", "vm_id": "vm-42", "vm_name": "mail",
        "ovf_descriptor": "<ovf>descriptor</ovf>", "files": [],
    })
    service.restore_to_esxi("ovf-backup", "mail-restored", "datastore1")
    assert FakeClient.imported[2:] == ("mail-restored", "datastore1")


def test_export_ova_streams_verified_files(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    chunks, size, _ = service.repository.store_stream(BytesIO(b"virtual-disk"))
    service.repository.write_manifest("ova-backup", {
        "format": 1, "backup_id": "ova-backup", "vm_id": "vm-42", "vm_name": "mail",
        "ovf_descriptor": "<Envelope>mail</Envelope>",
        "files": [{"name": "disk-01.vmdk", "size": size, "chunks": chunks}],
    })

    output = service.export_ova("ova-backup", tmp_path / "mail")

    with tarfile.open(output) as archive:
        assert archive.getnames() == ["mail.ovf", "disk-01.vmdk"]
        assert archive.extractfile("disk-01.vmdk").read() == b"virtual-disk"
