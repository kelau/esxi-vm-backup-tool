from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
import zstandard

pytest.importorskip("zstandard")

from esxi_backup.models import BackupRecord, BackupStatus
from esxi_backup.repository import BackupRepository


def test_chunks_are_compressed_deduplicated_and_restorable(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=4, level=1)
    chunks, logical, stored = repository.store_stream(
        BytesIO(b"abcdefghabcdefgh"), workers=3
    )

    assert logical == 16
    assert len(chunks) == 4
    assert len(list((tmp_path / "chunks").rglob("*.zst"))) == 2
    assert stored > 0

    output = BytesIO()
    repository.restore_stream(chunks, output)
    assert output.getvalue() == b"abcdefghabcdefgh"


def test_catalog_lifecycle(tmp_path):
    repository = BackupRepository(tmp_path)
    record = BackupRecord(id="one", vm_id="vm-1", vm_name="db", status=BackupStatus.RUNNING)
    repository.create(record)
    repository.finish("one", logical=100, stored=20)

    saved = repository.list("vm-1")[0]
    assert saved.status == BackupStatus.SUCCESS
    assert saved.finished_at is not None
    assert saved.logical_bytes == 100
    assert saved.stored_bytes == 20


def test_interrupted_backup_is_failed_when_repository_reopens(tmp_path):
    repository = BackupRepository(tmp_path)
    repository.create(BackupRecord(
        id="interrupted", vm_id="vm-1", vm_name="db", status=BackupStatus.RUNNING
    ))
    repository.db.close()

    reopened = BackupRepository(tmp_path)
    saved = reopened.list("vm-1")[0]

    assert saved.status == BackupStatus.FAILED
    assert saved.phase == "failed"
    assert saved.error == "Backup interrupted by service restart"


def test_legacy_null_metrics_are_normalized_on_read(tmp_path):
    repository = BackupRepository(tmp_path)
    values = BackupRecord(
        id="legacy", vm_id="vm-1", vm_name="db", status=BackupStatus.SUCCESS,
    ).model_dump(mode="json")
    values.update(throughput_mib_s=None, virtual_bytes=None, phase=None)

    saved = repository._backup_record(values)

    assert saved.throughput_mib_s == 0
    assert saved.virtual_bytes == 0
    assert saved.phase == "queued"


def test_catalog_reads_and_progress_writes_are_thread_safe(tmp_path):
    repository = BackupRepository(tmp_path)
    repository.create(BackupRecord(
        id="active", vm_id="vm-1", vm_name="db", status=BackupStatus.RUNNING
    ))

    def update(value):
        repository.update_progress(
            "active", progress=value % 95, phase="exporting",
            logical_bytes=value * 1024, throughput_mib_s=12.5,
        )
        return repository.list("vm-1")[0]

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(update, range(100)))

    assert all(record.id == "active" for record in records)
    assert all(record.vm_name == "db" for record in records)


def test_corrupt_chunk_is_rejected(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=64)
    chunks, _, _ = repository.store_stream(BytesIO(b"important"))
    digest = chunks[0]["sha256"]
    (repository.chunks / digest[:2] / f"{digest}.zst").write_bytes(b"corrupt")
    with pytest.raises(zstandard.ZstdError):
        repository.restore_stream(chunks, BytesIO())


def test_repository_stats_include_unique_chunks_and_recovery_points(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=4)
    repository.store_stream(BytesIO(b"abcdefgh"))
    repository.create(BackupRecord(
        id="done", vm_id="vm-1", vm_name="vm", status=BackupStatus.RUNNING
    ))
    repository.finish("done", logical=8, stored=8, virtual=1024)
    stats = repository.stats()
    assert stats["total_bytes"] >= stats["chunk_bytes"] > 0
    assert stats["recovery_points"] == 1
