from pathlib import Path


def test_linux_installer_has_atomic_release_and_auto_update_units():
    script = Path("scripts/install.sh").read_text(encoding="utf-8")

    assert "set -eu" in script
    assert "/releases/latest" in script
    assert "GITHUB_TOKEN" in script
    assert "application/vnd.github.raw+json" in script
    assert "source.tar.gz" in script
    assert "python3 -m venv" in script
    assert 'mv -Tf "$INSTALL_ROOT/current.new" "$INSTALL_ROOT/current"' in script
    assert "esxi-vm-backup-update.timer" in script
    assert "Persistent=true" in script
    assert "EnvironmentFile=-$CONFIG_DIR/update.env" in script
    assert "CHANGE-ME" in script
