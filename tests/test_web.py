from fastapi.testclient import TestClient

from esxi_backup.models import AppConfig, ServerConfig, VMInfo
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
