from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import zstandard

from .models import BackupRecord, BackupSchedule, BackupStatus, OvaExportRecord, RestoreRecord


class ChunkStream(io.RawIOBase):
    """Readable stream over verified repository chunks without materializing a full file."""

    def __init__(self, chunks):
        super().__init__()
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._finished = False

    def readable(self) -> bool:
        return True

    def readinto(self, destination) -> int:
        while len(self._buffer) < len(destination) and not self._finished:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                self._finished = True
        count = min(len(destination), len(self._buffer))
        destination[:count] = self._buffer[:count]
        del self._buffer[:count]
        return count


class BackupRepository:
    """Content-addressed, compressed chunk repository with SQLite metadata."""

    def __init__(self, root: Path, chunk_size: int = 8 * 1024 * 1024, level: int = 6):
        self.root = root.resolve()
        self.chunks = self.root / "chunks"
        self.manifests = self.root / "manifests"
        self.chunk_size = chunk_size
        self.compressor = zstandard.ZstdCompressor(level=level)
        self.decompressor = zstandard.ZstdDecompressor()
        self.chunks.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        # FastAPI executes synchronous routes in worker threads. SQLite serializes writes,
        # and this connection must therefore be allowed to follow the service across them.
        self.db = sqlite3.connect(self.root / "catalog.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS backups (
                id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, vm_name TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                logical_bytes INTEGER NOT NULL DEFAULT 0,
                stored_bytes INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE INDEX IF NOT EXISTS backups_vm ON backups(vm_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS schedules (
                vm_id TEXT PRIMARY KEY, vm_name TEXT NOT NULL,
                frequency TEXT NOT NULL, hour INTEGER NOT NULL,
                minute INTEGER NOT NULL, weekday INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ova_exports (
                backup_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, path TEXT, error TEXT,
                started_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS restores (
                backup_id TEXT PRIMARY KEY, vm_name TEXT NOT NULL,
                status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                error TEXT, started_at TEXT NOT NULL, finished_at TEXT
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(backups)")}
        for name, definition in {
            "progress": "INTEGER NOT NULL DEFAULT 0",
            "phase": "TEXT NOT NULL DEFAULT 'queued'",
            "current_file": "TEXT",
            "virtual_bytes": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE backups ADD COLUMN {name} {definition}")
        self.db.execute(
            "UPDATE backups SET phase='failed' WHERE status='failed' AND phase='queued'"
        )
        self.db.execute(
            """UPDATE backups SET phase='complete',progress=100
               WHERE status='success' AND phase='queued'"""
        )
        self.db.commit()

    def create(self, record: BackupRecord) -> None:
        values = record.model_dump(mode="json")
        self.db.execute(
            """INSERT INTO backups
               (id,vm_id,vm_name,status,started_at,finished_at,logical_bytes,
                stored_bytes,virtual_bytes,progress,phase,current_file,error)
               VALUES (:id,:vm_id,:vm_name,:status,:started_at,:finished_at,
                :logical_bytes,:stored_bytes,:virtual_bytes,:progress,:phase,
                :current_file,:error)""",
            values,
        )
        self.db.commit()

    def finish(
        self, backup_id: str, *, logical: int, stored: int, virtual: int = 0
    ) -> None:
        self.db.execute(
            """UPDATE backups SET status=?,finished_at=?,logical_bytes=?,stored_bytes=?,
               virtual_bytes=?,progress=100,phase='complete',current_file=NULL WHERE id=?""",
            (BackupStatus.SUCCESS, datetime.now(UTC).isoformat(), logical, stored,
             virtual, backup_id),
        )
        self.db.commit()

    def fail(self, backup_id: str, error: str) -> None:
        self.db.execute(
            """UPDATE backups SET status=?,finished_at=?,error=?,phase='failed',
               current_file=NULL WHERE id=?""",
            (BackupStatus.FAILED, datetime.now(UTC).isoformat(), error, backup_id),
        )
        self.db.commit()

    def list(self, vm_id: str | None = None) -> list[BackupRecord]:
        query = "SELECT * FROM backups"
        params: tuple[str, ...] = ()
        if vm_id:
            query += " WHERE vm_id=?"
            params = (vm_id,)
        rows = self.db.execute(query + " ORDER BY started_at DESC", params).fetchall()
        return [BackupRecord.model_validate(dict(row)) for row in rows]

    def update_progress(
        self, backup_id: str, *, progress: int, phase: str,
        current_file: str | None = None, logical_bytes: int | None = None,
    ) -> None:
        if logical_bytes is None:
            self.db.execute(
                "UPDATE backups SET progress=?,phase=?,current_file=? WHERE id=?",
                (max(0, min(100, progress)), phase, current_file, backup_id),
            )
        else:
            self.db.execute(
                """UPDATE backups SET progress=?,phase=?,current_file=?,logical_bytes=?
                   WHERE id=?""",
                (max(0, min(100, progress)), phase, current_file,
                 logical_bytes, backup_id),
            )
        self.db.commit()

    def store_stream(
        self, stream: BinaryIO, on_bytes: Callable[[int], None] | None = None
    ) -> tuple[list[dict[str, int | str]], int, int]:
        manifest: list[dict[str, int | str]] = []
        logical = stored = 0
        while data := stream.read(self.chunk_size):
            digest = hashlib.sha256(data).hexdigest()
            target = self.chunks / digest[:2] / f"{digest}.zst"
            logical += len(data)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                compressed = self.compressor.compress(data)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                    temp.write(compressed)
                    temp_path = Path(temp.name)
                os.replace(temp_path, target)
                stored += len(compressed)
            manifest.append({"sha256": digest, "size": len(data)})
            if on_bytes:
                on_bytes(len(data))
        return manifest, logical, stored

    def write_manifest(self, backup_id: str, document: dict) -> None:
        target = self.manifests / f"{backup_id}.json"
        target.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def restore_stream(self, chunks: Iterable[dict], output: BinaryIO) -> None:
        for data in self.iter_chunks(chunks):
            output.write(data)

    def iter_chunks(self, chunks: Iterable[dict]):
        for item in chunks:
            digest = str(item["sha256"])
            compressed = (self.chunks / digest[:2] / f"{digest}.zst").read_bytes()
            data = self.decompressor.decompress(compressed)
            if hashlib.sha256(data).hexdigest() != digest:
                raise OSError(f"Chunk integrity failure: {digest}")
            yield data

    def open_chunk_stream(self, chunks: Iterable[dict]) -> io.BufferedReader:
        return io.BufferedReader(ChunkStream(self.iter_chunks(chunks)))

    def save_schedule(self, schedule: BackupSchedule) -> None:
        self.db.execute(
            """INSERT INTO schedules(vm_id,vm_name,frequency,hour,minute,weekday)
               VALUES(?,?,?,?,?,?) ON CONFLICT(vm_id) DO UPDATE SET
               vm_name=excluded.vm_name,frequency=excluded.frequency,
               hour=excluded.hour,minute=excluded.minute,weekday=excluded.weekday""",
            (schedule.vm_id, schedule.vm_name, schedule.frequency, schedule.hour,
             schedule.minute, schedule.weekday),
        )
        self.db.commit()

    def list_schedules(self) -> list[BackupSchedule]:
        rows = self.db.execute("SELECT * FROM schedules ORDER BY vm_name").fetchall()
        return [BackupSchedule.model_validate(dict(row)) for row in rows]

    def get_schedule(self, vm_id: str) -> BackupSchedule | None:
        row = self.db.execute("SELECT * FROM schedules WHERE vm_id=?", (vm_id,)).fetchone()
        return BackupSchedule.model_validate(dict(row)) if row else None

    def start_ova_export(self, backup_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """INSERT INTO ova_exports(backup_id,status,progress,started_at)
               VALUES(?,?,0,?) ON CONFLICT(backup_id) DO UPDATE SET
               status=excluded.status,progress=0,path=NULL,error=NULL,
               started_at=excluded.started_at,finished_at=NULL""",
            (backup_id, BackupStatus.RUNNING, now),
        )
        self.db.commit()

    def update_ova_export(self, backup_id: str, progress: int) -> None:
        self.db.execute(
            "UPDATE ova_exports SET progress=? WHERE backup_id=?",
            (max(0, min(99, progress)), backup_id),
        )
        self.db.commit()

    def finish_ova_export(self, backup_id: str, path: Path) -> None:
        self.db.execute(
            """UPDATE ova_exports SET status=?,progress=100,path=?,finished_at=?
               WHERE backup_id=?""",
            (BackupStatus.SUCCESS, str(path), datetime.now(UTC).isoformat(), backup_id),
        )
        self.db.commit()

    def fail_ova_export(self, backup_id: str, error: str) -> None:
        self.db.execute(
            """UPDATE ova_exports SET status=?,error=?,finished_at=? WHERE backup_id=?""",
            (BackupStatus.FAILED, error, datetime.now(UTC).isoformat(), backup_id),
        )
        self.db.commit()

    def list_ova_exports(self) -> list[OvaExportRecord]:
        rows = self.db.execute("SELECT * FROM ova_exports").fetchall()
        return [OvaExportRecord.model_validate(dict(row)) for row in rows]

    def get_ova_export(self, backup_id: str) -> OvaExportRecord | None:
        row = self.db.execute(
            "SELECT * FROM ova_exports WHERE backup_id=?", (backup_id,)
        ).fetchone()
        return OvaExportRecord.model_validate(dict(row)) if row else None

    def start_restore(self, backup_id: str, vm_name: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """INSERT INTO restores(backup_id,vm_name,status,progress,started_at)
               VALUES(?,?,?,0,?) ON CONFLICT(backup_id) DO UPDATE SET
               vm_name=excluded.vm_name,status=excluded.status,progress=0,
               error=NULL,started_at=excluded.started_at,finished_at=NULL""",
            (backup_id, vm_name, BackupStatus.RUNNING, now),
        )
        self.db.commit()

    def update_restore(self, backup_id: str, progress: int) -> None:
        self.db.execute(
            "UPDATE restores SET progress=? WHERE backup_id=?",
            (max(0, min(99, progress)), backup_id),
        )
        self.db.commit()

    def finish_restore(self, backup_id: str) -> None:
        self.db.execute(
            """UPDATE restores SET status=?,progress=100,finished_at=?
               WHERE backup_id=?""",
            (BackupStatus.SUCCESS, datetime.now(UTC).isoformat(), backup_id),
        )
        self.db.commit()

    def fail_restore(self, backup_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE restores SET status=?,error=?,finished_at=? WHERE backup_id=?",
            (BackupStatus.FAILED, error, datetime.now(UTC).isoformat(), backup_id),
        )
        self.db.commit()

    def list_restores(self) -> list[RestoreRecord]:
        rows = self.db.execute("SELECT * FROM restores").fetchall()
        return [RestoreRecord.model_validate(dict(row)) for row in rows]
