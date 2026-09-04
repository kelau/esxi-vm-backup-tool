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
