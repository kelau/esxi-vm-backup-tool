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
