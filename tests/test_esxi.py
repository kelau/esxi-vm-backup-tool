from types import SimpleNamespace

from esxi_backup.esxi import EsxiClient
from esxi_backup.models import ServerConfig


def test_inventory_tolerates_vm_without_config():
    client = EsxiClient(ServerConfig(host="host", username="user", password="secret"))
    client._vms = lambda: [SimpleNamespace(
        _moId="vm-9", name="/vmfs/volumes/lost/inaccessible-vm.vmx", config=None,
        runtime=SimpleNamespace(powerState="poweredOff"),
    )]

    vm = client.list_vms()[0]

    assert vm.name == "inaccessible-vm"
    assert vm.reference == "/vmfs/volumes/lost/inaccessible-vm.vmx"
    assert vm.connection_state == "inaccessible"
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


def test_nfc_urls_with_vm_name_spaces_are_encoded():
    url = "https://esxi/ha-nfc/id/Usenet Indexers.nvram?token=a%20b"
    assert EsxiClient._safe_url(url) == (
        "https://esxi/ha-nfc/id/Usenet%20Indexers.nvram?token=a%20b"
    )
