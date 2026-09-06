from pathlib import Path


def test_linux_installer_has_atomic_release_and_auto_update_units():
    script = Path("scripts/install.sh").read_text(encoding="utf-8")

    assert "set -eu" in script
    assert "/releases/latest" in script
    assert "GITHUB_TOKEN" in script
    assert "application/vnd.github.raw+json" in script
    assert "source.tar.gz" in script
    assert "apt-get install -y curl ca-certificates python3 python3-venv python3-pip" in script
    assert "dnf install -y" in script
    assert "zypper --non-interactive install" in script
    assert "ufw allow" in script
    assert "firewall-cmd --permanent" in script
    assert 'WEB_PORT="${ESXI_BACKUP_WEB_PORT:-8080}"' in script
    assert 'install -d -o root -g "$SERVICE_USER" -m 0750 "$CONFIG_DIR"' in script
    assert 'chown root:"$SERVICE_USER" "$CONFIG_DIR/config.toml"' in script
    assert "python3 -m venv" in script
    assert 'python3 -m venv "$target"' in script
    assert 'mv "$temporary" "$target"' not in script
    assert 'rm -rf "$target"' in script
    assert 'mv -Tf "$INSTALL_ROOT/current.new" "$INSTALL_ROOT/current"' in script
    assert "esxi-vm-backup-update.timer" in script
    assert "Persistent=true" in script
    assert "EnvironmentFile=-$CONFIG_DIR/update.env" in script
    assert "CHANGE-ME" in script
