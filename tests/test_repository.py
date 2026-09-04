from io import BytesIO

import pytest
import zstandard

pytest.importorskip("zstandard")

from esxi_backup.models import BackupRecord, BackupStatus
from esxi_backup.repository import BackupRepository


def test_chunks_are_compressed_deduplicated_and_restorable(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=4, level=1)
    chunks, logical, stored = repository.store_stream(BytesIO(b"abcdefghabcdefgh"))

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


def test_corrupt_chunk_is_rejected(tmp_path):
    repository = BackupRepository(tmp_path, chunk_size=64)
    chunks, _, _ = repository.store_stream(BytesIO(b"important"))
    digest = chunks[0]["sha256"]
    (repository.chunks / digest[:2] / f"{digest}.zst").write_bytes(b"corrupt")
    with pytest.raises(zstandard.ZstdError):
        repository.restore_stream(chunks, BytesIO())
