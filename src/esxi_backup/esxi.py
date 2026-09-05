from __future__ import annotations

import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

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


class EsxiClient:
    """Small pyVmomi adapter. Keeping it isolated makes backup logic easily testable."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.si = None

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
            inventory.append(VMInfo(
                id=vm._moId,
                name=getattr(vm, "name", vm._moId),
                power_state=str(getattr(runtime, "powerState", "unknown")),
                connection_state=connection_state,
                guest_os=getattr(config, "guestFullName", None),
                provisioned_bytes=sum(
                    getattr(device, "capacityInBytes", 0) for device in devices
                ),
            ))
        return inventory

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
    def export(self, vm) -> Iterator[tuple[object, list[ExportFile]]]:
        lease = vm.ExportVm()
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
        try:
            yield lease, files
            lease.HttpNfcLeaseComplete()
        except Exception:
            lease.HttpNfcLeaseAbort()
            raise

    def open_export(self, url: str):
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
