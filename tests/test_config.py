from pathlib import Path

from esxi_backup.config import load_config, save_config
from esxi_backup.models import AppConfig, PortainerConfig, ServerConfig


def test_environment_password_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[server]\nhost="host"\nusername="user"\npassword="file"\n')
    monkeypatch.setenv("ESXI_BACKUP_PASSWORD", "environment")
    config = load_config(Path(path))
    assert config.server.password.get_secret_value() == "environment"


def test_save_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("ESXI_BACKUP_PASSWORD", raising=False)
    source = tmp_path / "config.toml"
    source.write_text('[server]\nhost="host"\nusername="user"\npassword="secret"\n')
    config = load_config(source)
    save_config(config, source)
    restored = load_config(source)
    assert restored == config


def test_save_config_serializes_portainer_api_key(tmp_path):
    path = tmp_path / "config.toml"
    config = AppConfig(
        server=ServerConfig(host="host", username="user", password="secret"),
        portainer=PortainerConfig(url="https://portainer.test", api_key="token"),
    )

    save_config(config, path)

    assert load_config(path).portainer.api_key.get_secret_value() == "token"
