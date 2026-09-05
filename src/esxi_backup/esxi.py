from __future__ import annotations

import base64
import hashlib
import re
import shlex
import socket
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

try:
    import paramiko
except ImportError:  # pragma: no cover - produces an actionable runtime error
    paramiko = None

from .models import ServerConfig, VMInfo


def wait_for_task(task) -> object:
    while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
        pass
    if task.info.state == vim.TaskInfo.State.error:
        raise task.info.error
    return task.info.result


@dataclass
class ExportFile:
    name: str
    url: str
    size: int
    device_id: str


class SnapshotExportUnsupported(RuntimeError):
    pass


def media_health(smart: dict[str, list[str]]) -> dict:
    """Derive a conservative, explainable media score from ESXi SMART rows."""
    if not smart:
        return {"score": None, "label": "Unknown", "notes": ["SMART data unavailable"]}

    def number(value: str, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    score = 100
    notes = []
    health = smart.get("Health Status", ["Unknown"])[0].upper()
    if health not in {"OK", "PASSED", "PASS"}:
        score = min(score, 20)
        notes.append(f"SMART health status is {health.title()}")
    wear = smart.get("Media Wearout Indicator")
    if wear:
        remaining = max(0, min(100, number(wear[0], 100)))
        score = min(score, remaining)
        notes.append(f"Media wear indicator: {remaining}%")
    severe = {
        "Pending Sector Reallocation Count", "Uncorrectable Sector Count",
        "Uncorrectable Error Count",
    }
    reallocations = {"Reallocated Sector Count", "Sector Reallocation Event Count"}
    errors = {
        "Program Fail Count", "Erase Fail Count", "Read Error Count", "Write Error Count",
    }
    for name in severe | reallocations | errors:
        values = smart.get(name)
        if not values:
            continue
        normalized = number(values[0], 100)
        threshold = number(values[1], 0) if len(values) > 1 else 0
        raw = number(values[-1], 0)
        if threshold and normalized <= threshold:
            score = min(score, 20)
            notes.append(f"{name} reached its SMART threshold")
        if raw <= 0:
            continue
        penalty = min(60, 25 + raw) if name in severe else (
            min(40, 10 + raw) if name in reallocations else min(25, 5 + raw)
        )
        score -= penalty
        notes.append(f"{name}: {raw}")
    score = max(0, min(100, score))
    label = "Healthy" if score >= 90 else "Watch" if score >= 70 else (
        "Warning" if score >= 40 else "Critical"
    )
    return {"score": score, "label": label, "notes": notes or ["No media faults reported"]}


def ssh_fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class PinnedHostKeyPolicy:
    def __init__(self, expected: str | None):
        self.expected = expected

    def missing_host_key(self, _client, hostname, key):
        fingerprint = ssh_fingerprint(key)
        if self.expected and key.get_base64() == self.expected:
            return
        if self.expected:
            raise paramiko.SSHException(
                f"SSH host key for {hostname} changed; presented fingerprint is {fingerprint}."
            )
        raise paramiko.SSHException(
            f"SSH host key for {hostname} is not trusted ({fingerprint}). "
            "Review and trust it on the Configuration page."
        )


class SftpExportStream:
    def __init__(self, sftp, handle, size: int):
        self.sftp = sftp
        self.handle = handle
        self.headers = {"Content-Length": str(size)}

    def read(self, size=-1):
        return self.handle.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.handle.close()
        self.sftp.close()


class EsxiClient:
    """Small pyVmomi adapter. Keeping it isolated makes backup logic easily testable."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.si = None
        self.ssh = None

    def __enter__(self):
        context = ssl.create_default_context()
        if not self.config.verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.si = SmartConnect(
            host=self.config.host, user=self.config.username,
            pwd=self.config.password.get_secret_value(), port=self.config.port,
            sslContext=context,
        )
        return self

    def __exit__(self, *_):
        if self.ssh:
            self.ssh.close()
            self.ssh = None
        if self.si:
            Disconnect(self.si)

    def _vms(self):
        view = self.si.content.viewManager.CreateContainerView(
            self.si.content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def list_vms(self) -> list[VMInfo]:
        inventory = []
        for vm in self._vms():
            config = getattr(vm, "config", None)
            hardware = getattr(config, "hardware", None)
            devices = getattr(hardware, "device", None) or []
            runtime = getattr(vm, "runtime", None)
            connection_state = str(getattr(runtime, "connectionState", "connected"))
            if config is None and connection_state == "connected":
                connection_state = "inaccessible"
            source_name = getattr(vm, "name", vm._moId)
            reference = None
            display_name = source_name
            if connection_state in {"inaccessible", "orphaned"}:
                path = PurePosixPath(str(source_name).replace("\\", "/"))
                if path.suffix.lower() == ".vmx":
                    display_name = path.stem
                    reference = str(source_name)
            inventory.append(VMInfo(
                id=vm._moId,
                name=display_name,
                power_state=str(getattr(runtime, "powerState", "unknown")),
                connection_state=connection_state,
                guest_os=getattr(config, "guestFullName", None),
                reference=reference,
                provisioned_bytes=sum(
                    getattr(device, "capacityInBytes", 0) for device in devices
                ),
            ))
        return inventory

    def storage_inventory(self, include_smart: bool = True) -> dict:
        """Return datastore, extent, device, and VM placement details."""
        content = self.si.RetrieveContent()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        try:
            hosts = list(view.view)
        finally:
            view.Destroy()
        datastores = {}
        devices = {}
        for host in hosts:
            storage = host.configManager.storageSystem
            luns = list(storage.storageDeviceInfo.scsiLun or [])
            lun_by_name = {
                str(getattr(lun, "canonicalName", "")): lun for lun in luns
            }
            extents_by_uuid = {}
            for mount in list(storage.fileSystemVolumeInfo.mountInfo or []):
                volume = mount.volume
                volume_uuid = str(getattr(volume, "uuid", ""))
                extents_by_uuid[volume_uuid] = [
                    str(extent.diskName)
                    for extent in list(getattr(volume, "extent", None) or [])
                ]
            for datastore in list(host.datastore or []):
                summary = datastore.summary
                datastore_uuid = str(summary.url).rstrip("/").split("/")[-1]
                extent_names = extents_by_uuid.get(datastore_uuid, [])
                entry = datastores.setdefault(datastore_uuid, {
                    "uuid": datastore_uuid, "name": summary.name, "type": summary.type,
                    "url": summary.url, "accessible": bool(summary.accessible),
                    "capacity_bytes": int(summary.capacity or 0),
                    "free_bytes": int(summary.freeSpace or 0), "devices": [], "vms": [],
                })
                for canonical_name in extent_names:
                    if canonical_name not in entry["devices"]:
                        entry["devices"].append(canonical_name)
                    lun = lun_by_name.get(canonical_name)
                    if lun and canonical_name not in devices:
                        capacity = getattr(lun, "capacity", None)
                        devices[canonical_name] = {
                            "canonical_name": canonical_name,
                            "display_name": getattr(lun, "displayName", None),
                            "vendor": getattr(lun, "vendor", None),
                            "model": getattr(lun, "model", None),
                            "revision": getattr(lun, "revision", None),
                            "serial_number": getattr(lun, "serialNumber", None),
                            "ssd": getattr(lun, "ssd", None),
                            "local_disk": getattr(lun, "localDisk", None),
                            "operational_state": list(
                                getattr(lun, "operationalState", None) or []
                            ),
                            "capacity_bytes": int(
                                getattr(capacity, "block", 0)
                                * getattr(capacity, "blockSize", 0)
                            ),
                            "smart": {}, "smart_error": None,
                        }
        for vm in self._vms():
            vm_entry = {
                "id": vm._moId, "name": vm.name,
                "power_state": str(getattr(vm.runtime, "powerState", "unknown")),
            }
            for datastore in list(getattr(vm, "datastore", None) or []):
                for entry in datastores.values():
                    if entry["name"] == datastore.name:
                        entry["vms"].append(vm_entry)
        if include_smart and devices:
            if not self.config.ssh_enabled:
                for device in devices.values():
                    device["smart_error"] = "Enable trusted SSH to read SMART data."
            else:
                ssh = self._connect_ssh()
                for canonical_name, device in devices.items():
                    try:
                        command = "esxcli storage core device smart get -d " + shlex.quote(
                            canonical_name
                        )
                        _stdin, stdout, stderr = ssh.exec_command(command, timeout=30)
                        output = stdout.read().decode("utf-8", "replace")
                        error = stderr.read().decode("utf-8", "replace").strip()
                        status = stdout.channel.recv_exit_status()
                        if status:
                            raise RuntimeError(error or f"esxcli exited with {status}")
                        lines = [line.strip() for line in output.splitlines() if line.strip()]
                        device["smart"] = {
                            parts[0]: parts[1:]
                            for line in lines[1:]
                            if len(parts := re.split(r"\s{2,}", line)) >= 2
                        }
                    except Exception as exc:
                        device["smart_error"] = str(exc)
        for device in devices.values():
            device["media_health"] = media_health(device["smart"])
        return {"host": self.config.host, "datastores": list(datastores.values()),
                "devices": list(devices.values())}

    def find_vm(self, identity: str):
        for vm in self._vms():
            if vm._moId == identity or vm.name == identity:
                return vm
        raise LookupError(f"VM not found: {identity}")

    def get_vm_details(self, identity: str) -> dict:
        vm = self.find_vm(identity)
        config = getattr(vm, "config", None)
        hardware = getattr(config, "hardware", None)
        guest = getattr(vm, "guest", None)
        summary = getattr(vm, "summary", None)
        storage = getattr(summary, "storage", None)
        disks = []
        for device in getattr(hardware, "device", None) or []:
            if isinstance(device, vim.vm.device.VirtualDisk):
                disks.append({
                    "label": getattr(device.deviceInfo, "label", "Disk"),
                    "capacity_bytes": int(getattr(device, "capacityInBytes", 0)),
                    "backing": getattr(device.backing, "fileName", None),
                })
        return {
            "id": vm._moId,
            "name": getattr(vm, "name", vm._moId),
            "power_state": str(getattr(getattr(vm, "runtime", None), "powerState", "unknown")),
            "guest_os": getattr(config, "guestFullName", None),
            "guest_hostname": getattr(guest, "hostName", None),
            "ip_address": getattr(guest, "ipAddress", None),
            "tools_status": str(getattr(guest, "toolsRunningStatus", "unknown")),
            "cpu": int(getattr(hardware, "numCPU", 0)),
            "memory_mib": int(getattr(hardware, "memoryMB", 0)),
            "firmware": getattr(config, "firmware", None),
            "uuid": getattr(config, "uuid", None),
            "datastores": [item.name for item in getattr(vm, "datastore", None) or []],
            "networks": [item.name for item in getattr(vm, "network", None) or []],
            "committed_bytes": int(getattr(storage, "committed", 0)),
            "uncommitted_bytes": int(getattr(storage, "uncommitted", 0)),
            "disks": disks,
        }

    def create_snapshot(self, vm, name: str, quiesce: bool):
        task = vm.CreateSnapshot_Task(
            name=name,
            description="ESXi Backup Tool",
            memory=False,
            quiesce=quiesce,
        )
        return wait_for_task(task)

    def remove_snapshot(self, snapshot) -> None:
        wait_for_task(snapshot.RemoveSnapshot_Task(removeChildren=False))

    @contextmanager
    def export(self, source) -> Iterator[tuple[object, list[ExportFile]]]:
        """Export a snapshot for hot backup, or a powered-off VM when requested directly."""
        try:
            export_snapshot = getattr(source, "ExportSnapshot", None)
            lease = export_snapshot() if export_snapshot else source.ExportVm()
        except vmodl.fault.NotSupported as exc:
            if getattr(source, "ExportSnapshot", None):
                raise SnapshotExportUnsupported from exc
            raise RuntimeError("This ESXi endpoint does not support VM export.") from exc
        except vim.fault.InvalidState as exc:
            raise RuntimeError(
                "ESXi refused the export because the VM or snapshot is not in an "
                "exportable state. Check that its configuration and datastore are accessible."
            ) from exc
        try:
            while lease.state == vim.HttpNfcLease.State.initializing:
                pass
            if lease.state == vim.HttpNfcLease.State.error:
                raise lease.error
            files = []
            disk_number = 0
            for index, device in enumerate(lease.info.deviceUrl, start=1):
                if getattr(device, "disk", False):
                    disk_number += 1
                    fallback_name = f"disk-{disk_number:02d}.vmdk"
                elif "nvram" in str(getattr(device, "importKey", "")).lower():
                    fallback_name = "vm.nvram"
                else:
                    fallback_name = f"artifact-{index:02d}.bin"
                files.append(ExportFile(
                    str(getattr(device, "targetId", None) or fallback_name),
                    device.url.replace("*", self.config.host),
                    int(device.fileSize or 0),
                    str(getattr(device, "key", None) or getattr(device, "importKey", index)),
                ))
            yield lease, files
            lease.HttpNfcLeaseComplete()
        except Exception:
            lease.HttpNfcLeaseAbort()
            raise

    def _connect_ssh(self):
        if not self.config.ssh_enabled:
            raise RuntimeError(
                "This standalone ESXi host requires the SSH fallback for hot backups. "
                "Enable it under Configuration after starting the ESXi SSH service."
            )
        if paramiko is None:
            raise RuntimeError("SSH hot backup requires the paramiko package.")
        client = paramiko.SSHClient()
        if self.config.ssh_verify_host_key:
            client.set_missing_host_key_policy(PinnedHostKeyPolicy(self.config.ssh_host_key))
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        password = self.config.ssh_password or self.config.password
        client.connect(
            hostname=self.config.host,
            port=self.config.ssh_port,
            username=self.config.ssh_username or self.config.username,
            password=password.get_secret_value(),
            timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        self.ssh = client
        return client

    def _open_transfer_sftp(self):
        """Open a high-bandwidth SFTP channel instead of Paramiko's small defaults."""
        transport = self.ssh.get_transport()
        return paramiko.SFTPClient.from_transport(
            transport,
            window_size=128 * 1024 * 1024,
            max_packet_size=1024 * 1024,
        )

    def inspect_ssh_host_key(self) -> tuple[str, str]:
        if paramiko is None:
            raise RuntimeError("SSH hot backup requires the paramiko package.")
        sock = socket.create_connection((self.config.host, self.config.ssh_port), timeout=15)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=15)
            key = transport.get_remote_server_key()
            return key.get_base64(), ssh_fingerprint(key)
        finally:
            transport.close()

    @staticmethod
    def _datastore_path(backing: str) -> tuple[str, str]:
        match = re.fullmatch(r"\[([^]]+)]\s+(.+)", backing)
        if not match or ".." in PurePosixPath(match.group(2)).parts:
            raise ValueError(f"Unsupported VMDK backing path: {backing}")
        return match.group(1), match.group(2)

    def _run_ssh(self, command: str, on_poll=None) -> None:
        _stdin, stdout, stderr = self.ssh.exec_command(command, timeout=3600)
        error_output = bytearray()
        while not stdout.channel.exit_status_ready():
            if on_poll:
                on_poll()
            while stdout.channel.recv_ready():
                stdout.channel.recv(65536)
            while stdout.channel.recv_stderr_ready():
                error_output.extend(stdout.channel.recv_stderr(65536))
            time.sleep(1)
        status = stdout.channel.recv_exit_status()
        if status:
            error_output.extend(stderr.read())
            message = error_output.decode("utf-8", "replace").strip()
            raise RuntimeError(f"ESXi vmkfstools failed: {message or f'exit {status}'}")

    @contextmanager
    def export_snapshot_ssh(self, snapshot, backup_id: str, on_prepare=None):
        ssh = self._connect_ssh()
        sftp = ssh.open_sftp()
        created: list[str] = []
        directories: set[str] = set()
        files: list[ExportFile] = []
        try:
            hardware = getattr(getattr(snapshot, "config", None), "hardware", None)
            disks = [
                device for device in (getattr(hardware, "device", None) or [])
                if isinstance(device, vim.vm.device.VirtualDisk)
            ]
            if not disks:
                raise RuntimeError("The snapshot contains no exportable virtual disks.")
            total_capacity = sum(int(getattr(disk, "capacityInBytes", 0)) for disk in disks)
            completed_capacity = 0
            for index, disk in enumerate(disks, start=1):
                datastore, relative = self._datastore_path(disk.backing.fileName)
                directory = f"/vmfs/volumes/{datastore}/.esxi-backup-{backup_id}"
                if directory not in directories:
                    sftp.mkdir(directory)
                    directories.add(directory)
                source = f"/vmfs/volumes/{datastore}/{relative}"
                destination = f"{directory}/disk-{index:02d}.vmdk"
                prefix = f"disk-{index:02d}"

                def report_clone_progress(
                    directory=directory, prefix=prefix,
                    completed_capacity=completed_capacity, index=index,
                ):
                    if not on_prepare:
                        return
                    current = sum(
                        int(item.st_size) for item in sftp.listdir_attr(directory)
                        if item.filename == f"{prefix}.vmdk"
                        or item.filename.startswith(f"{prefix}-s")
                    )
                    on_prepare(
                        min(total_capacity, completed_capacity + current),
                        total_capacity,
                        f"Disk {index} of {len(disks)}",
                    )

                self._run_ssh(
                    "vmkfstools -i " + shlex.quote(source) + " "
                    + shlex.quote(destination) + " -d 2gbsparse",
                    report_clone_progress,
                )
                completed_capacity += int(getattr(disk, "capacityInBytes", 0))
                clone_paths = sorted(
                    f"{directory}/{item.filename}"
                    for item in sftp.listdir_attr(directory)
                    if item.filename == f"{prefix}.vmdk"
                    or item.filename.startswith(f"{prefix}-s")
                )
                if not clone_paths:
                    raise RuntimeError(f"vmkfstools produced no files for {source}")
                for path in clone_paths:
                    created.append(path)
                    files.append(ExportFile(
                        name=PurePosixPath(path).name,
                        url=f"sftp:{path}",
                        size=int(sftp.stat(path).st_size),
                        device_id=str(disk.key),
                    ))
            yield None, files
        finally:
            for path in reversed(created):
                try:
                    sftp.remove(path)
                except OSError:
                    pass
            for directory in directories:
                try:
                    sftp.rmdir(directory)
                except OSError:
                    pass
            sftp.close()

    @contextmanager
    def export_hot(self, snapshot, backup_id: str, on_prepare=None):
        try:
            with self.export(snapshot) as result:
                yield result
        except SnapshotExportUnsupported:
            with self.export_snapshot_ssh(snapshot, backup_id, on_prepare) as result:
                yield result

    def open_export(self, url: str):
        if url.startswith("sftp:"):
            path = url.removeprefix("sftp:")
            sftp = self._open_transfer_sftp()
            size = int(sftp.stat(path).st_size)
            handle = sftp.open(path, "rb", bufsize=1024 * 1024)
            handle.prefetch(size, max_concurrent_requests=64)
            return SftpExportStream(sftp, handle, size)
        context = ssl.create_default_context()
        if not self.config.verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        request = Request(self._safe_url(url), headers={"Cookie": self.si._stub.cookie})
        return urlopen(request, context=context, timeout=300)

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit((
            parsed.scheme, parsed.netloc, quote(parsed.path, safe="/%:@"),
            parsed.query, parsed.fragment,
        ))

    def create_ovf_descriptor(self, vm, files: list[ExportFile]) -> str:
        params = vim.OvfManager.CreateDescriptorParams(
            includeImageFiles=False,
            ovfFiles=[vim.OvfManager.OvfFile(
                deviceId=item.device_id, path=item.name, size=item.size,
            ) for item in files]
        )
        result = self.si.content.ovfManager.CreateDescriptor(vm, params)
        if result.error:
            raise RuntimeError("; ".join(str(error) for error in result.error))
        return result.ovfDescriptor

    def import_ovf(
        self, descriptor: str, files: list[dict], name: str,
        chunk_reader, datastore_name: str | None = None, on_progress=None,
    ) -> None:
        datacenter = next(
            entity for entity in self.si.content.rootFolder.childEntity
            if isinstance(entity, vim.Datacenter)
        )
        compute = datacenter.hostFolder.childEntity[0]
        host = compute.host[0]
        resource_pool = compute.resourcePool
        datastores = list(host.datastore)
        datastore = next(
            (item for item in datastores if item.name == datastore_name), None
        ) if datastore_name else (datastores[0] if datastores else None)
        if datastore is None:
            raise LookupError(f"Datastore not found: {datastore_name}")
        params = vim.OvfManager.CreateImportSpecParams(
            entityName=name, hostSystem=host, diskProvisioning="thin"
        )
        result = self.si.content.ovfManager.CreateImportSpec(
            descriptor, resource_pool, datastore, params
        )
        if result.error:
            raise RuntimeError("; ".join(str(error) for error in result.error))
        lease = resource_pool.ImportVApp(result.importSpec, datacenter.vmFolder, host)
        while lease.state == vim.HttpNfcLease.State.initializing:
            pass
        if lease.state == vim.HttpNfcLease.State.error:
            raise lease.error
        by_device = {str(item.get("device_id")): item for item in files}
        by_name = {str(item["name"]): item for item in files}
        file_items = {str(item.deviceId): item for item in result.fileItem}
        total = sum(int(item["size"]) for item in files) or 1
        sent = 0
        try:
            for device in lease.info.deviceUrl:
                key = str(getattr(device, "importKey", None) or device.key)
                spec = file_items.get(key)
                file = by_device.get(key) or (by_name.get(spec.path) if spec else None)
                if file is None:
                    raise LookupError(f"No backup artifact for import device {key}")
                url = device.url.replace("*", self.config.host)
                def report(byte_count: int) -> None:
                    nonlocal sent
                    sent += byte_count
                    percent = min(99, int(sent * 100 / total))
                    lease.HttpNfcLeaseProgress(percent)
                    if on_progress:
                        on_progress(percent)

                self._upload(
                    url, chunk_reader(file["chunks"]), int(file["size"]), report
                )
            lease.HttpNfcLeaseComplete()
        except Exception:
            lease.HttpNfcLeaseAbort()
            raise

    def _upload(self, url: str, chunks, size: int, on_bytes=None) -> int:
        parsed = urlsplit(self._safe_url(url))
        context = ssl.create_default_context()
        if not self.config.verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connection_class = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        kwargs = {"context": context} if parsed.scheme == "https" else {}
        connection = connection_class(parsed.hostname, parsed.port, timeout=300, **kwargs)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection.putrequest("PUT", path)
        connection.putheader("Cookie", self.si._stub.cookie)
        connection.putheader("Content-Length", str(size))
        connection.endheaders()
        sent = 0
        for chunk in chunks:
            connection.send(chunk)
            sent += len(chunk)
            if on_bytes:
                on_bytes(len(chunk))
        response = connection.getresponse()
        response.read()
        connection.close()
        if response.status >= 300:
            raise OSError(f"ESXi upload failed with HTTP {response.status}")
        return sent
