from __future__ import annotations

import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
            inventory.append(VMInfo(
                id=vm._moId,
                name=getattr(vm, "name", vm._moId),
                power_state=str(getattr(runtime, "powerState", "unknown")),
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
        files = [ExportFile(
            str(getattr(d, "deviceId", None) or getattr(d, "importKey", None)
                or getattr(d, "key", "export")),
            d.url.replace("*", self.config.host),
            d.fileSize,
        ) for d in lease.info.deviceUrl]
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
        request = Request(url, headers={"Cookie": self.si._stub.cookie})
        return urlopen(request, context=context, timeout=300)
