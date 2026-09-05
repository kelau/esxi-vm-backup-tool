# ESXi VM Backup Tool

A small Python backup service for standalone VMware ESXi hosts. It creates a VM snapshot,
exports a consistent OVF/VMDK image while the VM stays online, splits the stream into
content-addressed chunks, compresses only new chunks, and always removes its temporary snapshot.
It includes an automation-friendly CLI, JSON API, and web dashboard.

> **Project status:** early release. Test restores before relying on it. VMware's free ESXi
> license may restrict the APIs required for backup; use a licensed host with API access.

## Why backups stay small

- Fixed-size SHA-256 chunks are stored once across all VMs and backup generations.
- Zstandard compression is applied to each new chunk.
- A bounded pipeline overlaps network reads, hashing, compression, and repository writes.
- Multiple virtual disks can be exported concurrently with a configurable safety limit.
- Identical blocks in successive full exports are referenced, not copied.
- Manifests are tiny JSON documents, so each recovery point is independent even though its data
  is deduplicated.

The ESXi export itself is a full image. Network transfer is therefore not incremental in this
first release, but repository growth is. A future CBT transport can reduce transfer time further.

For faster backups, tune `chunk_size_mib`, `pipeline_workers` (1–8), and `parallel_disks` (1–4)
on the Settings page. A practical starting point is 32 MiB, 2 pipeline workers, and 2 parallel
disks. Increase these cautiously while watching ESXi load, CPU, memory, and repository I/O. The
dashboard reports live aggregate throughput in MiB/s. SSH hot backups use a 128 MiB receive
window and bounded SFTP read-ahead to avoid latency-bound small reads.

## Install

Python 3.11+ is required.

```bash
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e .
cp config.example.toml config.toml
```

Edit `config.toml`. For scheduled jobs, omit the real password from disk and provide
`ESXI_BACKUP_PASSWORD`. If ESXi uses a self-signed certificate, install its CA certificate;
`verify_ssl = false` is available for isolated test environments but is not recommended.

Standalone ESXi may expose `ExportSnapshot` but reject it at runtime. For hot backups in that
case, enable the ESXi SSH service and configure `ssh_enabled`, `ssh_username`, and
`ssh_password` (or `ESXI_BACKUP_SSH_PASSWORD`). The SSH account must be allowed to run
`vmkfstools`. Keep host-key verification enabled and add the host key to `known_hosts`.

## CLI and automation

```bash
esxi-backup vms
esxi-backup backup "my-vm"
esxi-backup history --json
esxi-backup restore BACKUP_ID ./recovered
esxi-backup restore-to-esxi BACKUP_ID --name "Recovered VM" --datastore datastore1
esxi-backup export-ova BACKUP_ID ./Recovered-VM.ova
esxi-backup web --host 0.0.0.0 --port 8080
```

All commands accept `--config PATH`. Alternatively set `ESXI_BACKUP_CONFIG`. Commands return a
non-zero status on failure, making them suitable for cron, systemd timers, Task Scheduler, or a
CI runner. Example cron job (daily at 02:15):

```cron
15 2 * * * /usr/local/sbin/run-esxi-backup vm-42
```

The root-owned wrapper should set `ESXI_BACKUP_CONFIG` and obtain `ESXI_BACKUP_PASSWORD` from your
secret manager before executing the command; do not place the password in the crontab.

## Web UI and API

Run `esxi-backup web`, then open `http://localhost:8080`. The dashboard shows VMs, power state,
latest recovery point, storage consumed, failures, OVA build progress, and a **Back up now** action.
The Schedules page manages named daily or weekly policies. A policy can contain multiple VMs, a VM
can belong to multiple policies, and each policy controls snapshot quiescing and optional automatic
OVA packaging. Existing per-VM schedules migrate to single-VM named policies. Schedules persist in
the repository and resume when the web service restarts. Configuration (with passwords excluded)
is available at `GET /api/v1/config`.

The VM table's **Backup size** is the compressed footprint of all unique chunks referenced by that
recovery point. **New data** in Recent jobs is only the additional repository space written during
that run; shared chunks mean deleting one recovery point may reclaim less than its displayed
footprint.

API endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/vms` | VM inventory |
| `GET` | `/api/v1/backups` | Backup history and status |
| `GET` | `/api/v1/config` | Effective non-secret configuration |
| `GET` | `/api/v1/schedules` | Named schedule policies and next run times |
| `POST` | `/api/v1/vms/{id}/backups` | Queue a backup |

Put the web service behind an authenticated TLS reverse proxy before exposing it beyond a trusted
management network. Bind to `127.0.0.1` (the default) otherwise.

## Backup sequence and consistency

1. Connect to ESXi through the vSphere API.
2. Create a quiesced snapshot without VM memory. VMware Tools must be installed for application-
   aware filesystem quiescing. Set `quiesce = false` if unavailable.
3. Try an HTTP NFC snapshot-export lease. If standalone ESXi does not implement it and the SSH
   fallback is enabled, use `vmkfstools` to create temporary split sparse clones from
   the snapshot disk chain and transfer them over SFTP.
4. Hash, compress, and atomically persist chunks; write the recovery-point manifest.
5. Complete the lease or remove temporary SSH clones, then remove the snapshot in `finally`
   blocks, including after failures.

Snapshot lifetime increases consolidation risk. Monitor datastore free space, keep jobs short, and
alert on failed snapshot removal. Database servers may need guest-native pre/post freeze hooks for
transaction-level guarantees.

## Restore

New recovery points capture a native OVF descriptor. Use `restore-to-esxi` to verify and stream their
chunks directly into an HTTP NFC import lease under a new VM name; the command never overwrites an
existing VM. The datastore option is optional and defaults to the first datastore visible to the
host. `esxi-backup restore` remains available to reconstruct VMDK/NVRAM files offline.

Recovery points made before OVF capture (format version 1 without `ovf_descriptor`) require the
manual disk-attach workflow: reconstruct `disk-01.vmdk`, create a replacement VM with matching
firmware and controller type, convert/upload the VMDK, and attach it as an existing disk.

`export-ova` creates a portable OVA on demand by streaming verified chunks directly into its tar
archive. The compact repository remains the primary storage format; retaining an OVA for every
recovery point would duplicate full virtual disks and defeat cross-backup deduplication.

SSH hot-backup recovery points contain a VMDK descriptor plus 2 GiB sparse extents. Use offline
restore to reconstruct the set, then run `vmkfstools -i disk-01.vmdk recovered.vmdk -d thin` on
ESXi and attach `recovered.vmdk` to a replacement VM. OVA and direct NFC restore are unavailable
for these points because the standalone host cannot produce a stream-optimized snapshot export.

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest --cov=esxi_backup
```

Tests use an in-memory ESXi adapter and do not require a hypervisor. See
[`docs/architecture.md`](docs/architecture.md) and [`docs/operations.md`](docs/operations.md).

## Versioning

The project follows semantic versioning and is currently pre-1.0. Version `0.5.2` marks the first
52-commit development milestone. From this baseline, feature releases increment the minor version,
fix-only releases increment the patch version, and `1.0.0` will mark a stable repository format and
supported upgrade path. Run `esxi-backup --version` to see the installed version.

## Security

- Create a dedicated least-privilege ESXi account with VM snapshot, export, inventory, and lease
  permissions. The SSH fallback additionally needs an ESXi shell account permitted to run
  `vmkfstools`; restrict and protect it carefully.
- Store the repository on encrypted, access-controlled storage and copy it off-host.
- Protect configuration permissions and inject credentials through a secret manager.
- The chunk hash is an integrity check, not a signature. Use filesystem immutability or object-lock
  replication for ransomware resistance.

## License

MIT
