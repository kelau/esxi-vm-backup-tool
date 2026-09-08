from esxi_backup.portainer import PortainerClient


def test_volume_sizes_include_large_volumes_and_deduplicate():
    mounts = [{"Type": "volume", "Name": "birdnet"}] * 2
    assert PortainerClient.persistent_size(mounts, {"birdnet": 300 * 1024**3}) == 300 * 1024**3


def test_unknown_usage_is_not_reported_as_zero():
    assert PortainerClient.persistent_size([{"Type": "bind"}], {}) is None
    mounts = [{"Type": "volume", "Name": "missing"}]
    assert PortainerClient.persistent_size(mounts, {}) is None
    assert PortainerClient.persistent_size(mounts, {"missing": -1}) is None
    assert PortainerClient.persistent_size([], {}) == 0
