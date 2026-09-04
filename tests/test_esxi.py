from types import SimpleNamespace

from esxi_backup.esxi import EsxiClient
from esxi_backup.models import ServerConfig


def test_inventory_tolerates_vm_without_config():
    client = EsxiClient(ServerConfig(host="host", username="user", password="secret"))
    client._vms = lambda: [SimpleNamespace(
        _moId="vm-9", name="inaccessible-vm", config=None,
        runtime=SimpleNamespace(powerState="poweredOff"),
    )]

    vm = client.list_vms()[0]

    assert vm.name == "inaccessible-vm"
    assert vm.power_state == "poweredOff"
    assert vm.provisioned_bytes == 0


def test_ovf_descriptor_explicitly_excludes_iso_images():
    captured = {}

    class OvfManager:
        def CreateDescriptor(self, _vm, params):
            captured["params"] = params
            return SimpleNamespace(error=[], ovfDescriptor="<Envelope/>")

    client = EsxiClient(ServerConfig(host="host", username="user", password="secret"))
    client.si = SimpleNamespace(content=SimpleNamespace(ovfManager=OvfManager()))
    descriptor = client.create_ovf_descriptor(SimpleNamespace(), [])

    assert descriptor == "<Envelope/>"
    assert captured["params"].includeImageFiles is False
