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


def test_read_progress_is_finer_than_repository_chunks(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=2 * 1024 * 1024, level=1)
    reads = []

    chunks, logical, _stored = repository.store_stream(
        BytesIO(b"x" * (2 * 1024 * 1024 + 7)), on_read=reads.append
    )

    assert logical == 2 * 1024 * 1024 + 7
    assert len(chunks) == 2
    assert reads == [1024 * 1024, 1024 * 1024, 7]


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


def test_existing_manifest_repository_size_is_backfilled(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=4)
    chunks, _, _ = repository.store_stream(BytesIO(b"abcdefgh"))
    repository.create(BackupRecord(
        id="existing", vm_id="vm-1", vm_name="vm", status=BackupStatus.SUCCESS
    ))
    repository.write_manifest("existing", {
        "backup_id": "existing", "files": [{"chunks": chunks}],
    })
    expected = sum({chunk["sha256"]: chunk["stored_size"] for chunk in chunks}.values())
    repository.db.close()

    reopened = BackupRepository(tmp_path)

    assert reopened.list("vm-1")[0].repository_bytes == expected


def test_delete_vm_preserves_shared_chunks_and_removes_unique_data(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=4)
    shared, _, _ = repository.store_stream(BytesIO(b"same"))
    unique, _, _ = repository.store_stream(BytesIO(b"only"))
    for backup_id, vm_id, chunks in (
        ("one", "vm-1", shared + unique), ("two", "vm-2", shared),
    ):
        repository.create(BackupRecord(
            id=backup_id, vm_id=vm_id, vm_name=vm_id, status=BackupStatus.SUCCESS
        ))
        repository.write_manifest(backup_id, {
            "backup_id": backup_id, "vm_id": vm_id, "files": [{"chunks": chunks}],
        })

    result = repository.delete_vm("vm-1")

    assert result["recovery_points"] == 1
    assert repository.list("vm-1") == []
    assert len(repository.list("vm-2")) == 1
    assert (repository.chunks / shared[0]["sha256"][:2] /
            f"{shared[0]['sha256']}.zst").exists()
    assert not (repository.chunks / unique[0]["sha256"][:2] /
                f"{unique[0]['sha256']}.zst").exists()


def test_secondary_repository_receives_recovery_data_and_catalog(tmp_path):
    primary, secondary = tmp_path / "primary", tmp_path / "secondary"
    repository = BackupRepository(primary, chunk_size=4, secondary_root=secondary)
    chunks, _, _ = repository.store_stream(BytesIO(b"disk-data"))
    repository.create(BackupRecord(
        id="backup-1", vm_id="vm-1", vm_name="demo", status=BackupStatus.SUCCESS
    ))
    repository.write_manifest("backup-1", {
        "backup_id": "backup-1", "vm_id": "vm-1",
        "files": [{"name": "disk.vmdk", "chunks": chunks}],
    })

    assert repository.sync_mirror()

    mirrored = BackupRepository(secondary)
    assert mirrored.list("vm-1")[0].id == "backup-1"
    assert (secondary / "manifests" / "backup-1.json").is_file()
    assert len(list((secondary / "chunks").rglob("*.zst"))) == len(
        list((primary / "chunks").rglob("*.zst"))
    )

    repository.db.close()
    (primary / "catalog.sqlite3").unlink()

    assert repository.availability_error() is None
    assert repository.root == secondary
    assert repository.list("vm-1")[0].id == "backup-1"
