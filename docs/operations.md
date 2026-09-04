# Operations guide

## Service account

Create a dedicated ESXi role limited to inventory read, snapshot create/remove, and VM export/NFC
lease operations. Apply it only to VMs that should be protected. Exact privilege names can differ by
ESXi/vCenter version; validate with a non-production VM.

## Scheduling

Schedule one `esxi-backup backup VM_ID` command per VM. Stagger large VMs. Capture stdout/stderr and
alert on a non-zero exit. Avoid overlapping jobs until repository inter-process locking is added.

## Capacity planning

Allow space for unique compressed chunks plus temporary ESXi snapshot deltas. `stored_bytes` is the
new physical data written by a run; `logical_bytes` is its reconstructed export size. A high ratio of
new to logical bytes indicates changed or poorly deduplicating workloads (encrypted disks commonly
behave this way).

## Failure recovery

- A failed record remains in the catalog with its error.
- The snapshot removal is attempted regardless of export success.
- Orphan chunks are harmless but consume space. Garbage collection is not yet implemented.
- If snapshot cleanup itself fails, remove the `esxi-backup-*` snapshot in vSphere after confirming
  no backup is running.

## Restore drill

At least quarterly, restore the newest recovery point to a staging datastore, import its OVF under a
new name on an isolated network, boot it, and perform application-level validation. A successful
backup job alone does not prove recoverability.

For OVF-enabled recovery points, run `esxi-backup restore-to-esxi BACKUP_ID --name NEW_NAME` and add
`--datastore NAME` when the host has multiple datastores. The tool verifies every chunk while
streaming it to an ESXi import lease and creates a new VM; it never replaces an existing VM.

For older format-version-1 points without an OVF descriptor, run `esxi-backup restore BACKUP_ID
DESTINATION`, create a replacement VM with matching firmware and virtual disk-controller type,
upload/convert the reconstructed VMDK, and attach it as an existing disk. Keep the original VM
powered off but intact until validation succeeds.
