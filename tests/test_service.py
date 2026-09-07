import json
import tarfile
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest

pytest.importorskip("zstandard")

from esxi_backup.models import AppConfig, PortainerConfig, ServerConfig, VMInfo
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
            runtime=SimpleNamespace(powerState="poweredOn"),
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
    def export(self, snapshot):
        assert snapshot is not self.vm
        yield FakeLease(), [SimpleNamespace(
            name="disk.vmdk", url="memory://disk", size=12, device_id="disk-1"
        )]

    def export_hot(self, snapshot, _backup_id, _on_prepare=None):
        return self.export(snapshot)

    def create_ovf_descriptor(self, _vm, _exports):
        return "<ovf>descriptor</ovf>"

    def open_export(self, _url):
        return BytesIO(b"virtual disk")

    def import_ovf(self, descriptor, files, name, chunk_reader, datastore, on_progress=None):
        FakeClient.imported = (descriptor, files, name, datastore)
        if on_progress:
            on_progress(75)


class CancelStream(BytesIO):
    def __init__(self, data, cancel):
        super().__init__(data)
        self.cancel = cancel
        self.triggered = False

    def read(self, size=-1):
        data = super().read(size)
        if data and not self.triggered:
            self.triggered = True
            self.cancel()
        return data


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
    assert record.expected_bytes == 1024
    assert record.repository_bytes > 0
    assert record.virtual_bytes == 1024
    assert FakeClient.removed
    assert (tmp_path / "manifests" / f"{record.id}.json").exists()


def test_powered_off_backup_exports_vm_without_snapshot(tmp_path):
    class ColdClient(FakeClient):
        def __init__(self, config):
            super().__init__(config)
            self.vm.runtime.powerState = "poweredOff"

        def create_snapshot(self, *_args):
            raise AssertionError("powered-off VM should not be snapshotted")

        @contextmanager
        def export(self, source):
            assert source is self.vm
            yield FakeLease(), [SimpleNamespace(
                name="disk.vmdk", url="memory://disk", size=12, device_id="disk-1"
            )]

    service = BackupService(config(tmp_path), client_factory=ColdClient)
    assert service.backup("mail").status == "success"


def test_ssh_backup_uses_sparse_clone_size_as_transfer_total(tmp_path):
    class SshClient(FakeClient):
        @contextmanager
        def export_hot(self, _snapshot, _backup_id, _on_prepare=None):
            yield None, [SimpleNamespace(
                name="disk-01-s001.vmdk", url="memory://disk",
                size=12, device_id="disk-1",
            )]

    service = BackupService(config(tmp_path), client_factory=SshClient)
    record = service.backup("mail")

    assert record.expected_bytes == 12


def test_backup_can_be_cancelled_and_still_removes_snapshot(tmp_path):
    service = None

    class CancelClient(FakeClient):
        def open_export(self, _url):
            def cancel():
                active = service.repository.list("vm-42")[0]
                assert service.cancel_backup(active.id)
            return CancelStream(b"virtual disk", cancel)

    service = BackupService(config(tmp_path), client_factory=CancelClient)
    record = service.backup("mail")

    assert record.status == "cancelled"
    assert record.phase == "cancelled"
    assert FakeClient.removed


def test_list_vms(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    assert service.list_vms()[0].name == "mail"


def test_duplicate_backup_for_same_vm_is_rejected(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    service._begin_backup("existing", "vm-42", "mail")

    with pytest.raises(RuntimeError, match="already running for mail"):
        service.backup("mail")


def test_host_wide_backup_concurrency_limit_is_enforced(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    service._begin_backup("existing", "vm-other", "another VM")

    assert "concurrency limit reached (1)" in service.backup_capacity_error("vm-42")
    with pytest.raises(RuntimeError, match=r"concurrency limit reached \(1\)"):
        service.backup("mail")


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


def test_restore_preserves_split_sparse_vmdk_names(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    descriptor, _, _ = service.repository.store_stream(BytesIO(b"descriptor"))
    extent, _, _ = service.repository.store_stream(BytesIO(b"extent"))
    service.repository.write_manifest("ssh-backup", {
        "format": 1, "transport": "ssh-2gbsparse", "backup_id": "ssh-backup",
        "vm_id": "vm-42", "vm_name": "mail", "files": [
            {"name": "disk-01.vmdk", "chunks": descriptor},
            {"name": "disk-01-s001.vmdk", "chunks": extent},
        ],
    })

    outputs = service.restore("ssh-backup", tmp_path / "split-output")

    assert [path.name for path in outputs] == ["disk-01.vmdk", "disk-01-s001.vmdk"]


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


def test_chunk_stream_reports_bytes_as_they_are_read(tmp_path):
    service = BackupService(config(tmp_path), client_factory=FakeClient)
    chunks, _, _ = service.repository.store_stream(BytesIO(b"virtual-disk"))
    reads = []

    with service.repository.open_chunk_stream(chunks, on_read=reads.append) as stream:
        assert stream.read() == b"virtual-disk"

    assert sum(reads) == len(b"virtual-disk")


def test_container_backup_captures_metadata_and_named_volumes(tmp_path, monkeypatch):
    events = []

    class FakePortainer:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def inspect_container(self, _endpoint, _container):
            return {
                "Name": "/database", "Image": "postgres@sha256:test",
                "State": {"Running": True},
                "Mounts": [
                    {"Type": "volume", "Name": "db-data", "Destination": "/data"},
                    {"Type": "bind", "Source": "/host/secrets", "Destination": "/secrets"},
                ],
            }

        def pause(self, *_):
            events.append("pause")

        def unpause(self, *_):
            events.append("unpause")

        @contextmanager
        def archive(self, _endpoint, _container, path):
            events.append(path)
            yield BytesIO(b"volume archive")

    app_config = config(tmp_path)
    app_config.portainer = PortainerConfig(
        url="https://portainer.test", api_key="token", include_bind_mounts=False
    )
    service = BackupService(app_config, client_factory=FakeClient)
    monkeypatch.setattr("esxi_backup.service.PortainerClient", FakePortainer)

    backup = service.backup_container(1, "container-id")
    manifest = json.loads(
        (service.repository.manifests / f"{backup.id}.json").read_text(encoding="utf-8")
    )

    assert backup.status == "success"
    assert events == ["pause", "/data", "unpause"]
    assert manifest["kind"] == "docker-container"
    assert [item["name"] for item in manifest["files"]] == ["db-data.tar"]
