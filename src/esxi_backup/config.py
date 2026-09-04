from __future__ import annotations

import os
import tomllib
from pathlib import Path

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
    return AppConfig.model_validate(data)

