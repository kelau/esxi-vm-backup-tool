# Architecture

The code deliberately separates the vSphere adapter, orchestration service, and repository. This
keeps VMware objects out of persistence code and lets all hot-backup behavior be unit tested.

```text
CLI / FastAPI dashboard
         |
   BackupService
    /          \
EsxiClient   BackupRepository
(pyVmomi)    (SQLite + zstd chunks + JSON manifests)
```

The web service also owns a single APScheduler instance. Per-VM daily or weekly definitions are
stored in SQLite and rebuilt at startup. Jobs coalesce missed runs and allow only one running
instance per VM.

## Storage layout

```text
repository/
  catalog.sqlite3
  chunks/ab/abcdef...zst
  manifests/<backup-id>.json
```

Chunks are addressed by SHA-256 of uncompressed content. A temporary file is compressed in the
destination directory and atomically renamed, so interruption cannot expose a partial chunk under
its final name. A manifest lists ordered chunks for each exported file. SQLite stores operational
state and dashboard history; manifests and chunks contain everything necessary for reconstruction.

## Concurrency

SQLite provides transactional job-state writes. Chunk creation is safe for a single service
process. Before running multiple workers against one repository, add an inter-process lock or use
atomic exclusive creation. The recommended deployment is one worker with backup jobs serialized by
the scheduler, limiting ESXi snapshot and datastore pressure.

## Roadmap

- Changed Block Tracking transport for truly incremental network reads
- Retention pruning with chunk reference garbage collection
- Repository encryption and signed manifests
- Native OVF upload restore workflow
- Per-VM schedules, notifications, and job cancellation
- Metrics and snapshot-age watchdog
