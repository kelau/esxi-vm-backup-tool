from fastapi.testclient import TestClient

from esxi_backup.models import AppConfig, BackupRecord, BackupStatus, ServerConfig, VMInfo
from esxi_backup.service import BackupService
from esxi_backup.web import create_app


class FakeClient:
    def __init__(self, _config):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def list_vms(self):
        return [VMInfo(id="vm-1", name="demo", power_state="poweredOn")]


def test_dashboard_renders_from_worker_thread(tmp_path, monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="esxi.test", username="user", password="secret"),
        repository=str(tmp_path),
    )
    service = BackupService(config, client_factory=FakeClient)
    monkeypatch.setattr("esxi_backup.web.BackupService", lambda _config: service)
    monkeypatch.setattr("esxi_backup.web.load_config", lambda _path: config)

    response = TestClient(create_app()).get("/")

    assert response.status_code == 200
    assert "demo" in response.text
    assert "esxi.test" in response.text
    assert "refreshDashboard" in response.text
    assert "VM size" in response.text
    assert "Actions for demo" in response.text


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
        "keep_last": "5", "keep_daily": "10", "keep_weekly": "4", "keep_monthly": "6",
    }, follow_redirects=False)
    assert response.status_code == 303
    saved = config_path.read_text(encoding="utf-8")
    assert 'host = "new.test"' in saved
    assert 'password = "secret"' in saved


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
