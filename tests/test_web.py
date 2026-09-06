from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from esxi_backup.models import (
    AppConfig,
    BackupRecord,
    BackupStatus,
    SchedulePolicy,
    ServerConfig,
    VMInfo,
)
from esxi_backup.service import BackupService
from esxi_backup.web import create_app


class FakeClient:
    imported = None
    def __init__(self, _config):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def list_vms(self):
        return [
            VMInfo(id="vm-1", name="demo", power_state="poweredOn"),
            VMInfo(
                id="vm-broken", name="missing-vmx", power_state="unknown",
                connection_state="inaccessible",
            ),
        ]

    def get_vm_details(self, identity):
        if identity != "vm-1":
            raise LookupError(identity)
        return {
            "id": identity, "name": "demo", "power_state": "poweredOn",
            "guest_os": "Linux", "guest_hostname": "demo", "ip_address": "192.0.2.1",
            "tools_status": "guestToolsRunning", "cpu": 2, "memory_mib": 2048,
            "firmware": "efi", "uuid": "uuid-1", "datastores": ["datastore1"],
            "networks": ["VM Network"], "committed_bytes": 1024,
            "uncommitted_bytes": 2048, "disks": [],
        }

    def import_ovf(self, descriptor, files, name, chunk_reader, datastore, on_progress=None):
        FakeClient.imported = (name, datastore)
        if on_progress:
            on_progress(80)

    def inspect_ssh_host_key(self):
        return "encoded-host-key", "SHA256:test-fingerprint"

    def storage_inventory(self, include_smart=True):
        return {
            "host": "esxi.test",
            "datastores": [{
                "uuid": "uuid-ds", "name": "datastore1", "type": "VMFS",
                "url": "/vmfs/volumes/uuid-ds", "accessible": True,
                "capacity_bytes": 1000, "free_bytes": 400,
                "devices": ["naa.test"],
                "vms": [{"id": "vm-1", "name": "demo", "power_state": "poweredOn"}],
            }],
            "devices": [{
                "canonical_name": "naa.test", "display_name": "Test SSD",
                "vendor": "TEST", "model": "FAST", "revision": "1",
                "serial_number": "SERIAL", "ssd": True, "local_disk": True,
                "operational_state": ["ok"], "capacity_bytes": 1000,
                "smart": {"Health Status": ["OK"]}, "smart_error": None,
                "media_health": {"score": 100, "label": "Healthy", "notes": []},
            }],
        }


def test_dashboard_renders_from_worker_thread(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    service.repository.create(BackupRecord(
        id="running-1", vm_id="vm-1", vm_name="demo", status=BackupStatus.RUNNING,
        phase="exporting", logical_bytes=2 * 1024**3, expected_bytes=4 * 1024**3,
        throughput_mib_s=12.5,
    ))
    service.repository.create(BackupRecord(
        id="deleted-1", vm_id="vm-deleted", vm_name="deleted-demo",
        status=BackupStatus.SUCCESS, virtual_bytes=10 * 1024**3,
    ))
    service.repository.write_manifest("deleted-1", {
        "backup_id": "deleted-1", "vm_id": "vm-deleted", "vm_name": "deleted-demo",
        "ovf_descriptor": "<Envelope/>", "files": [],
    })
    service.repository.start_ova_export("deleted-1")
    service.repository.update_ova_export("deleted-1", 12.34)
    service.repository.save_schedule_policy(SchedulePolicy(
        id="nightly", name="Nightly", vm_ids=["vm-1"], frequency="daily",
    ))
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    response = TestClient(create_app()).get("/")

    assert response.status_code == 200
    assert "v0.8.1" in response.text
    assert 'href="/datastores"' in response.text
    assert "demo" in response.text
    assert "esxi.test" in response.text
    assert "refreshDashboard" in response.text
    assert "VM size" in response.text
    assert "Actions for demo" in response.text
    assert "details.actions[open]" in response.text
    assert "showVmDetails" in response.text
    assert "if (row && !interactive) showVmDetails" in response.text
    assert "error-brief" in response.text
    assert "Total repository" in response.text
    assert "menu.removeAttribute('open')" in response.text
    assert "Hide failed" in response.text
    assert "hideFailedJobs" in response.text
    assert "bar.indeterminate" in response.text
    assert "2 GiB" in response.text
    assert "Live on ESXi" in response.text
    assert "Backup only" in response.text
    assert "Inaccessible on ESXi" in response.text
    assert "deleted-demo" in response.text
    assert 'data-vm-state="backup_only"' in response.text
    assert "Remove all backups" in response.text
    assert "Restore to ESXi" in response.text
    assert "localizeTimes" in response.text
    assert "applyVmSort" in response.text
    assert 'href="/schedules"' in response.text
    assert "enhanceOvaProgress" in response.text
    assert ">12.3%</span>" in response.text
    assert 'data-progress="12.34"' in response.text
    assert "ova-progress" not in response.text
    assert "estimating time remaining" in response.text
    assert 'data-sort="backupSize"' in response.text
    assert 'data-sort="schedule"' in response.text
    assert 'aria-label="Scheduled: Nightly"' in response.text
    assert "backup-progress-row" in response.text
    assert "Backup running" in response.text
    assert "throughput-chart" in response.text
    assert "formatDuration" in response.text
    assert "Estimated remaining" in response.text
    assert "updateRemainingTimes" in response.text
    assert 'class="vm-name"' in response.text
    assert 'class="vm-os"' in response.text
    assert 'class="relative-backup-time"' in response.text
    assert "daysAgo === 0" in response.text
    assert '<span class="success">success</span>' not in response.text


def test_vm_details_api_combines_esxi_and_backup_data(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    response = TestClient(create_app()).get("/api/v1/vms/vm-1")

    assert response.status_code == 200
    assert response.json()["cpu"] == 2
    assert response.json()["backups"] == []


def test_dashboard_survives_disconnected_repository(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    monkeypatch.setattr(
        service.repository, "availability_error", lambda: "Backup repository is unavailable"
    )
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)
    client = TestClient(create_app())

    dashboard = client.get("/")
    repository = client.get("/api/v1/repository")

    assert dashboard.status_code == 200
    assert "Backup storage disconnected" in dashboard.text
    assert "demo" in dashboard.text
    assert "Actions are disabled" in dashboard.text
    assert repository.status_code == 503
    assert "unavailable" in repository.json()["detail"]


def test_datastores_page_persists_inventory_snapshot(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    client = TestClient(create_app())
    initial = client.get("/datastores")
    refresh = client.post("/api/v1/storage/refresh")
    status = client.get("/api/v1/storage/refresh-status")
    response = client.get("/datastores")

    assert initial.status_code == 200
    assert "No storage snapshot is available yet" in initial.text
    assert refresh.status_code == 202
    assert refresh.json()["accepted"] is True
    assert status.json()["status"] == "success"
    assert response.status_code == 200
    assert "Refreshing inventory in the background" in response.text
    assert "storageRefreshPending" in response.text
    assert "datastore1" in response.text
    assert "Test SSD" in response.text
    assert "Vendor / model" in response.text
    assert "TEST FAST" in response.text
    assert "Health Status" in response.text
    assert "Healthy media health" in response.text
    assert "demo" in response.text
    assert service.repository.latest_storage_snapshot()["host"] == "esxi.test"


def test_repository_only_vm_details_do_not_require_esxi_vm(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    service.repository.create(BackupRecord(
        id="backup-1", vm_id="vm-deleted", vm_name="deleted-demo",
        status=BackupStatus.SUCCESS, virtual_bytes=10 * 1024**3,
    ))
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    response = TestClient(create_app()).get(
        "/api/v1/vms/vm-deleted?repository_only=true"
    )

    assert response.status_code == 200
    assert response.json()["inventory_state"] == "backup_only"
    assert response.json()["name"] == "deleted-demo"
    assert len(response.json()["backups"]) == 1


def test_settings_update_keeps_masked_password(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="old.test", username="user", password="secret"),
        repository=str(tmp_path / "repo"),
    )
    service = BackupService(config, client_factory=FakeClient)
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)
    app = create_app(config_path)
    response = TestClient(app).post("/settings", data={
        "host": "new.test", "port": "443", "username": "new-user", "password": "",
        "verify_ssl": "true", "repository": str(tmp_path / "new-repo"),
        "chunk_size_mib": "16", "compression_level": "8", "quiesce": "true",
        "pipeline_workers": "3", "parallel_disks": "2",
        "keep_last": "5", "keep_daily": "10", "keep_weekly": "4", "keep_monthly": "6",
    }, follow_redirects=False)
    assert response.status_code == 303
    saved = config_path.read_text(encoding="utf-8")
    assert 'host = "new.test"' in saved
    assert 'password = "secret"' in saved


def test_config_api_excludes_both_passwords(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(
            host="esxi.test", username="user", password="api-secret",
            ssh_password="ssh-secret",
        ),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    server = TestClient(create_app()).get("/api/v1/config").json()["server"]

    assert "password" not in server
    assert "ssh_password" not in server


def test_home_assistant_api_reports_backup_health(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    finished_at = datetime.now(UTC) - timedelta(hours=2)
    service.repository.create(BackupRecord(
        id="backup-1", vm_id="vm-1", vm_name="demo", status=BackupStatus.SUCCESS,
        finished_at=finished_at, repository_bytes=1234, virtual_bytes=4096,
    ))
    service.repository.save_schedule_policy(SchedulePolicy(
        id="nightly", name="Nightly", vm_ids=["vm-1"], frequency="daily",
    ))
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    client = TestClient(create_app())
    summary = client.get("/api/v1/home-assistant").json()
    vm = client.get("/api/v1/home-assistant/vms/vm-1").json()

    assert summary["status"] == "ok"
    assert summary["esxi_connected"] is True
    assert summary["repository_available"] is True
    assert summary["protected_vm_count"] == 1
    assert summary["scheduled_vm_count"] == 1
    assert summary["repository"]["recovery_points"] == 1
    assert vm["backup_state"] == "protected"
    assert vm["backup_size_bytes"] == 1234
    assert vm["last_backup_age_seconds"] >= 7200
    assert vm["schedules"] == ["Nightly"]


def test_home_assistant_api_can_omit_vm_attributes(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    response = TestClient(create_app()).get("/api/v1/home-assistant?include_vms=false")

    assert response.status_code == 200
    assert "vms" not in response.json()


def test_settings_can_pin_current_esxi_ssh_key(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path / "repo"),
    )
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda updated: BackupService(
        updated, client_factory=FakeClient
    ))
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    response = TestClient(create_app(config_path)).post(
        "/settings/trust-ssh-host", follow_redirects=False
    )

    assert response.status_code == 303
    assert 'ssh_host_key = "encoded-host-key"' in config_path.read_text(encoding="utf-8")


def test_web_can_build_and_download_ova(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    service.repository.create(BackupRecord(
        id="backup-1", vm_id="vm-1", vm_name="demo", status=BackupStatus.SUCCESS,
    ))
    service.repository.write_manifest("backup-1", {
        "format": 1, "backup_id": "backup-1", "vm_id": "vm-1", "vm_name": "demo",
        "ovf_descriptor": "<Envelope/>", "files": [],
    })
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    with TestClient(create_app()) as client:
        response = client.post("/backups/backup-1/ova", follow_redirects=False)
        download = client.get("/backups/backup-1/ova")

    assert response.status_code == 303
    assert service.repository.get_ova_export("backup-1").status == "success"
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/x-virtualization-ova"
    assert 'filename="demo.ova"' in download.headers["content-disposition"]


def test_web_can_restore_specific_recovery_point(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    service.repository.write_manifest("backup-1", {
        "format": 1, "backup_id": "backup-1", "vm_id": "vm-1", "vm_name": "demo",
        "ovf_descriptor": "<Envelope/>", "files": [],
    })
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    with TestClient(create_app()) as client:
        response = client.post(
            "/backups/backup-1/restore-to-esxi",
            data={"name": "demo-previous", "datastore": "datastore1"},
        )

    restore = service.repository.list_restores()[0]
    assert response.status_code == 202
    assert restore.status == "success"
    assert restore.progress == 100
    assert FakeClient.imported == ("demo-previous", "datastore1")
