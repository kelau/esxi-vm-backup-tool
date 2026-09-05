from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

import tomli_w
from platformdirs import user_config_path

from .models import AppConfig

DEFAULT_CONFIG_PATH = user_config_path("esxi-vm-backup") / "config.toml"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or Path(os.environ.get("ESXI_BACKUP_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"Configuration not found: {path}. Copy config.example.toml and set credentials."
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    # Environment variables are convenient for unattended jobs and keep secrets out of files.
    if password := os.environ.get("ESXI_BACKUP_PASSWORD"):
        data.setdefault("server", {})["password"] = password
    if ssh_password := os.environ.get("ESXI_BACKUP_SSH_PASSWORD"):
        data.setdefault("server", {})["ssh_password"] = ssh_password
    return AppConfig.model_validate(data)


def resolve_config_path(path: Path | None = None) -> Path:
    return path or Path(os.environ.get("ESXI_BACKUP_CONFIG", DEFAULT_CONFIG_PATH))


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Atomically persist configuration while preserving a private file mode on POSIX."""
    target = resolve_config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="python", exclude_none=True)
    data["server"]["password"] = config.server.password.get_secret_value()
    if config.server.ssh_password:
        data["server"]["ssh_password"] = config.server.ssh_password.get_secret_value()
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(tomli_w.dumps(data).encode("utf-8"))
    temporary.chmod(0o600)
    os.replace(temporary, target)
    return target
