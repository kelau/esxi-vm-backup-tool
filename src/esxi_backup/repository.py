from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import zstandard

from .models import BackupRecord, BackupStatus


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
        """)
        self.db.commit()

    def create(self, record: BackupRecord) -> None:
        values = record.model_dump(mode="json")
        self.db.execute(
            "INSERT INTO backups VALUES (:id,:vm_id,:vm_name,:status,:started_at,"
            ":finished_at,:logical_bytes,:stored_bytes,:error)", values
        )
        self.db.commit()

    def finish(self, backup_id: str, *, logical: int, stored: int) -> None:
        self.db.execute(
            "UPDATE backups SET status=?,finished_at=?,logical_bytes=?,stored_bytes=? WHERE id=?",
            (BackupStatus.SUCCESS, datetime.now(UTC).isoformat(), logical, stored, backup_id),
        )
        self.db.commit()

    def fail(self, backup_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE backups SET status=?,finished_at=?,error=? WHERE id=?",
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

    def store_stream(self, stream: BinaryIO) -> tuple[list[dict[str, int | str]], int, int]:
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
        return manifest, logical, stored

    def write_manifest(self, backup_id: str, document: dict) -> None:
        target = self.manifests / f"{backup_id}.json"
        target.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def restore_stream(self, chunks: Iterable[dict], output: BinaryIO) -> None:
        for item in chunks:
            digest = str(item["sha256"])
            compressed = (self.chunks / digest[:2] / f"{digest}.zst").read_bytes()
            data = self.decompressor.decompress(compressed)
            if hashlib.sha256(data).hexdigest() != digest:
                raise OSError(f"Chunk integrity failure: {digest}")
            output.write(data)
