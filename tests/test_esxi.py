from types import SimpleNamespace

from esxi_backup.esxi import EsxiClient, SftpExportStream, media_health
from esxi_backup.models import ServerConfig


def test_media_health_uses_wear_and_fault_counters():
    healthy = media_health({
        "Health Status": ["OK"], "Media Wearout Indicator": ["93", "0", "93", "114"],
        "Reallocated Sector Count": ["100", "10", "100", "0"],
    })
    damaged = media_health({
        "Health Status": ["OK"], "Pending Sector Reallocation Count": ["99", "0", "99", "2"],
    })

    assert healthy == {
        "score": 93, "label": "Healthy", "notes": ["Media wear indicator: 93%"]
    }
    assert damaged["score"] == 73
    assert damaged["label"] == "Watch"


def test_media_health_ignores_esxi_zero_placeholder_normalized_values():
    health = media_health({
        "Health Status": ["OK", "N/A", "N/A", "N/A"],
        "Read Error Count": ["0", "51", "N/A", "0"],
        "Write Error Count": ["0", "0", "N/A", "0"],
        "Reallocated Sector Count": ["0", "140", "N/A", "0"],
        "Sector Reallocation Event Count": ["0", "0", "N/A", "0"],
        "Pending Sector Reallocation Count": ["0", "0", "N/A", "0"],
        "Uncorrectable Sector Count": ["0", "0", "N/A", "0"],
    })

    assert health == {
        "score": 100, "label": "Healthy", "notes": ["No media faults reported"]
    }


def test_normal_ssd_wear_does_not_trigger_watch_too_early():
    health = media_health({
        "Health Status": ["OK", "N/A", "N/A", "N/A"],
        "Media Wearout Indicator": ["79", "0", "79", "441"],
        "Write Error Count": ["100", "10", "100", "0"],
        "Reallocated Sector Count": ["100", "10", "100", "0"],
        "Program Fail Count": ["100", "10", "100", "0"],
        "Erase Fail Count": ["100", "10", "100", "0"],
        "Uncorrectable Error Count": ["100", "0", "100", "0"],
    })

    assert health == {
        "score": 79, "label": "Healthy", "notes": ["Media wear indicator: 79%"]
    }


def test_sftp_export_uses_bounded_readv_windows():
    class Handle:
        calls = []

        def readv(self, chunks, max_concurrent_prefetch_requests=None):
            self.calls.append((chunks, max_concurrent_prefetch_requests))
            return [b"x" * length for _offset, length in chunks]

        def close(self):
            pass

    class Sftp:
        def close(self):
            pass

    handle = Handle()
    stream = SftpExportStream(Sftp(), handle, 20 * 1024 * 1024)

    assert len(stream.read(1024 * 1024)) == 1024 * 1024
    assert len(handle.calls) == 1
    assert sum(length for _offset, length in handle.calls[0][0]) == 8 * 1024 * 1024
    assert handle.calls[0][1] == 8
    assert len(stream.buffer) == 7 * 1024 * 1024
    assert len(stream.read()) == 19 * 1024 * 1024


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


def test_esxi_datastore_path_is_converted_safely():
    assert EsxiClient._datastore_path("[DS3] Cookie Clicker/disk-000001.vmdk") == (
        "DS3", "Cookie Clicker/disk-000001.vmdk"
    )


def test_hot_clone_uses_source_when_safe_and_falls_back_to_roomiest_datastore():
    def datastore(name, free, accessible=True):
        return SimpleNamespace(
            name=name,
            summary=SimpleNamespace(name=name, freeSpace=free, accessible=accessible),
        )

    source = datastore("DS3", 20)
    roomy = datastore("DS4", 100)
    offline = datastore("DS5", 1000, accessible=False)
    snapshot = SimpleNamespace(vm=SimpleNamespace(
        runtime=SimpleNamespace(host=SimpleNamespace(datastore=[source, roomy, offline]))
    ))

    assert EsxiClient._select_clone_datastore(snapshot, "DS3", 10) == "DS3"
    assert EsxiClient._select_clone_datastore(snapshot, "DS3", 50) == "DS4"


def test_hot_clone_reports_when_no_datastore_has_enough_space():
    datastore = SimpleNamespace(
        name="DS3", summary=SimpleNamespace(name="DS3", freeSpace=20, accessible=True)
    )
    snapshot = SimpleNamespace(vm=SimpleNamespace(
        runtime=SimpleNamespace(host=SimpleNamespace(datastore=[datastore]))
    ))

    try:
        EsxiClient._select_clone_datastore(snapshot, "DS3", 50)
    except RuntimeError as error:
        assert "No accessible ESXi datastore" in str(error)
    else:
        raise AssertionError("Expected insufficient datastore space to be rejected")
