from pathlib import Path

from esxi_backup.config import load_config


def test_environment_password_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[server]\nhost="host"\nusername="user"\npassword="file"\n')
    monkeypatch.setenv("ESXI_BACKUP_PASSWORD", "environment")
    config = load_config(Path(path))
    assert config.server.password.get_secret_value() == "environment"

