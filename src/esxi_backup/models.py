from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr


class BackupStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ServerConfig(BaseModel):
    host: str
    username: str
    password: SecretStr
    port: Annotated[int, Field(ge=1, le=65535)] = 443
    verify_ssl: bool = True


class RetentionConfig(BaseModel):
    keep_last: Annotated[int, Field(ge=1)] = 7
    keep_daily: Annotated[int, Field(ge=0)] = 14
    keep_weekly: Annotated[int, Field(ge=0)] = 8
    keep_monthly: Annotated[int, Field(ge=0)] = 12


class AppConfig(BaseModel):
    server: ServerConfig
    repository: str = "./backups"
    chunk_size_mib: Annotated[int, Field(ge=1, le=256)] = 8
    compression_level: Annotated[int, Field(ge=1, le=19)] = 6
    pipeline_workers: Annotated[int, Field(ge=1, le=8)] = 2
    parallel_disks: Annotated[int, Field(ge=1, le=4)] = 2
    quiesce: bool = True
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class VMInfo(BaseModel):
    id: str
    name: str
    power_state: str
    guest_os: str | None = None
    provisioned_bytes: int = 0


class BackupRecord(BaseModel):
    id: str
    vm_id: str
    vm_name: str
    status: BackupStatus
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    logical_bytes: int = 0
    stored_bytes: int = 0
    virtual_bytes: int = 0
    throughput_mib_s: float = 0
    progress: Annotated[int, Field(ge=0, le=100)] = 0
    phase: str = "queued"
    current_file: str | None = None
    error: str | None = None


class BackupSchedule(BaseModel):
    vm_id: str
    vm_name: str
    frequency: Literal["disabled", "daily", "weekly"] = "disabled"
    hour: Annotated[int, Field(ge=0, le=23)] = 2
    minute: Annotated[int, Field(ge=0, le=59)] = 0
    weekday: Annotated[int, Field(ge=0, le=6)] = 0
    next_run_at: datetime | None = None


class OvaExportRecord(BaseModel):
    backup_id: str
    status: BackupStatus
    progress: Annotated[int, Field(ge=0, le=100)] = 0
    path: str | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class RestoreRecord(BaseModel):
    backup_id: str
    vm_name: str
    status: BackupStatus
    progress: Annotated[int, Field(ge=0, le=100)] = 0
    error: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
