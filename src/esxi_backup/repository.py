from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
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
        self.compression_level = level
        self.decompressor = zstandard.ZstdDecompressor()
        self._write_lock = threading.Lock()
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
            CREATE TABLE IF NOT EXISTS chunk_index (
                sha256 TEXT PRIMARY KEY, logical_size INTEGER NOT NULL DEFAULT 0,
                stored_size INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS repository_meta (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL
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
            "throughput_mib_s": "REAL NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE backups ADD COLUMN {name} {definition}")
        ova_columns = {row[1] for row in self.db.execute("PRAGMA table_info(ova_exports)")}
        if "size_bytes" not in ova_columns:
            self.db.execute(
                "ALTER TABLE ova_exports ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0"
            )
        self.db.execute(
            "UPDATE backups SET phase='failed' WHERE status='failed' AND phase='queued'"
        )
        self.db.execute(
            """UPDATE backups SET phase='complete',progress=100
               WHERE status='success' AND phase='queued'"""
        )
        self.db.execute(
            """UPDATE backups SET virtual_bytes=COALESCE(virtual_bytes,0),
               throughput_mib_s=COALESCE(throughput_mib_s,0),
               progress=COALESCE(progress,0),phase=COALESCE(phase,'queued')"""
        )
        self.db.execute(
            """UPDATE backups SET status='failed',phase='failed',finished_at=?,
               current_file=NULL,
               error=COALESCE(error,'Backup interrupted by service restart')
               WHERE status='running'""",
            (datetime.now(UTC).isoformat(),),
        )
        self.db.commit()
        self._backfill_repository_index()

    def _backfill_repository_index(self) -> None:
        initialized = self.db.execute(
            "SELECT value FROM repository_meta WHERE key='index_initialized'"
        ).fetchone()
        if initialized:
            return
        logical_sizes = {}
        manifest_bytes = 0
        for manifest_path in self.manifests.glob("*.json"):
            manifest_bytes += manifest_path.stat().st_size
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for file in manifest.get("files", []):
                    for chunk in file.get("chunks", []):
                        logical_sizes[str(chunk["sha256"])] = int(chunk.get("size", 0))
            except (OSError, ValueError, KeyError):
                continue
        for chunk_path in self.chunks.rglob("*.zst"):
            digest = chunk_path.stem
            self.db.execute(
                """INSERT OR IGNORE INTO chunk_index(sha256,logical_size,stored_size)
                   VALUES(?,?,?)""",
                (digest, logical_sizes.get(digest, 0), chunk_path.stat().st_size),
            )
        for row in self.db.execute(
            "SELECT backup_id,path FROM ova_exports WHERE path IS NOT NULL"
        ):
            path = Path(row["path"])
            if path.is_file():
                self.db.execute(
                    "UPDATE ova_exports SET size_bytes=? WHERE backup_id=?",
                    (path.stat().st_size, row["backup_id"]),
                )
        self.db.execute(
            "INSERT OR REPLACE INTO repository_meta(key,value) VALUES('manifest_bytes',?)",
            (manifest_bytes,),
        )
        self.db.execute(
            "INSERT INTO repository_meta(key,value) VALUES('index_initialized',1)"
        )
        self.db.commit()

    def create(self, record: BackupRecord) -> None:
        values = record.model_dump(mode="json")
        self.db.execute(
            """INSERT INTO backups
               (id,vm_id,vm_name,status,started_at,finished_at,logical_bytes,
                stored_bytes,virtual_bytes,throughput_mib_s,progress,phase,current_file,error)
               VALUES (:id,:vm_id,:vm_name,:status,:started_at,:finished_at,
                :logical_bytes,:stored_bytes,:virtual_bytes,:throughput_mib_s,
                :progress,:phase,
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
        return [self._backup_record(row) for row in rows]

    @staticmethod
    def _backup_record(row) -> BackupRecord:
        values = dict(row)
        # Catalogs created by older versions can contain NULL in columns added
        # later. Normalize defensively even if a migration was interrupted.
        for field, default in {
            "logical_bytes": 0, "stored_bytes": 0, "virtual_bytes": 0,
            "throughput_mib_s": 0.0, "progress": 0, "phase": "queued",
        }.items():
            if values.get(field) is None:
                values[field] = default
        return BackupRecord.model_validate(values)

    def update_progress(
        self, backup_id: str, *, progress: int, phase: str,
        current_file: str | None = None, logical_bytes: int | None = None,
        throughput_mib_s: float = 0,
    ) -> None:
        if logical_bytes is None:
            self.db.execute(
                """UPDATE backups SET progress=?,phase=?,current_file=?,throughput_mib_s=?
                   WHERE id=?""",
                (max(0, min(100, progress)), phase, current_file,
                 throughput_mib_s, backup_id),
            )
        else:
            self.db.execute(
                """UPDATE backups SET progress=?,phase=?,current_file=?,logical_bytes=?,
                   throughput_mib_s=?
                   WHERE id=?""",
                (max(0, min(100, progress)), phase, current_file,
                 logical_bytes, throughput_mib_s, backup_id),
            )
        self.db.commit()

    def store_stream(
        self, stream: BinaryIO, on_bytes: Callable[[int], None] | None = None,
        workers: int = 2,
    ) -> tuple[list[dict[str, int | str]], int, int]:
        manifest: list[dict[str, int | str]] = []
        logical = stored = 0
        pending = deque()

        def prepare(data: bytes):
            digest = hashlib.sha256(data).hexdigest()
            compressed = zstandard.ZstdCompressor(
                level=self.compression_level
            ).compress(data)
            return digest, len(data), compressed

        def consume(future):
            nonlocal logical, stored
            digest, data_size, compressed = future.result()
            target = self.chunks / digest[:2] / f"{digest}.zst"
            with self._write_lock:
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                        temp.write(compressed)
                        temp_path = Path(temp.name)
                    os.replace(temp_path, target)
                    stored += len(compressed)
                self.db.execute(
                    """INSERT OR IGNORE INTO chunk_index(sha256,logical_size,stored_size)
                       VALUES(?,?,?)""",
                    (digest, data_size, target.stat().st_size),
                )
            logical += data_size
            manifest.append({"sha256": digest, "size": data_size})
            if on_bytes:
                on_bytes(data_size)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            while data := stream.read(self.chunk_size):
                pending.append(executor.submit(prepare, data))
                if len(pending) >= workers * 2:
                    consume(pending.popleft())
            while pending:
                consume(pending.popleft())
        return manifest, logical, stored

    def write_manifest(self, backup_id: str, document: dict) -> None:
        target = self.manifests / f"{backup_id}.json"
        previous_size = target.stat().st_size if target.exists() else 0
        target.write_text(json.dumps(document, indent=2), encoding="utf-8")
        size_delta = target.stat().st_size - previous_size
        self.db.execute(
            """INSERT INTO repository_meta(key,value) VALUES('manifest_bytes',?)
               ON CONFLICT(key) DO UPDATE SET value=value+excluded.value""",
            (size_delta,),
        )
        self.db.commit()

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
            """UPDATE ova_exports SET status=?,progress=100,path=?,size_bytes=?,finished_at=?
               WHERE backup_id=?""",
            (BackupStatus.SUCCESS, str(path), path.stat().st_size,
             datetime.now(UTC).isoformat(), backup_id),
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

    def stats(self) -> dict[str, int]:
        chunk_bytes = self.db.execute(
            "SELECT COALESCE(SUM(stored_size),0) FROM chunk_index"
        ).fetchone()[0]
        manifest_row = self.db.execute(
            "SELECT value FROM repository_meta WHERE key='manifest_bytes'"
        ).fetchone()
        manifest_bytes = manifest_row[0] if manifest_row else 0
        ova_bytes = self.db.execute(
            "SELECT COALESCE(SUM(size_bytes),0) FROM ova_exports WHERE status='success'"
        ).fetchone()[0]
        page_count = self.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        catalog_bytes = page_count * page_size
        return {
            "total_bytes": chunk_bytes + manifest_bytes + ova_bytes + catalog_bytes,
            "chunk_bytes": chunk_bytes,
            "manifest_bytes": manifest_bytes,
            "ova_bytes": ova_bytes,
            "catalog_bytes": catalog_bytes,
            "recovery_points": self.db.execute(
                "SELECT COUNT(*) FROM backups WHERE status='success'"
            ).fetchone()[0],
        }

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
