#!/bin/sh
set -eu

REPOSITORY="${ESXI_BACKUP_GITHUB_REPO:-kelau/esxi-vm-backup-tool}"
INSTALL_ROOT="${ESXI_BACKUP_INSTALL_ROOT:-/opt/esxi-vm-backup}"
CONFIG_DIR="${ESXI_BACKUP_CONFIG_DIR:-/etc/esxi-vm-backup}"
DATA_DIR="${ESXI_BACKUP_DATA_DIR:-/var/lib/esxi-vm-backup}"
SERVICE_USER="${ESXI_BACKUP_USER:-esxi-backup}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
UPDATE_ONLY=false
[ "${1:-}" = "--update" ] && UPDATE_ONLY=true

fail() { printf '%s\n' "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail "Run this installer as root (for example: curl ... | sudo sh)."
command -v curl >/dev/null 2>&1 || fail "curl is required."
command -v python3 >/dev/null 2>&1 || fail "Python 3.11 or newer is required."
command -v systemctl >/dev/null 2>&1 || fail "systemd is required."
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
  || fail "Python 3.11 or newer is required."

github_curl() {
  if [ -n "$GITHUB_TOKEN" ]; then
    curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" "$@"
  else
    curl -fsSL "$@"
  fi
}

release_json="$(github_curl -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  "https://api.github.com/repos/${REPOSITORY}/releases/latest")" \
  || fail "Could not read the latest GitHub release."
tag="$(printf '%s' "$release_json" | sed -n \
  's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
[ -n "$tag" ] || fail "The latest GitHub release has no tag."
version="${tag#v}"

current_version=""
if [ -x "$INSTALL_ROOT/current/bin/esxi-backup" ]; then
  current_version="$($INSTALL_ROOT/current/bin/esxi-backup --version 2>/dev/null || true)"
fi
if [ "$UPDATE_ONLY" = true ] && [ "$current_version" = "$version" ]; then
  printf 'ESXi VM Backup Tool %s is already current.\n' "$version"
  exit 0
fi

target="$INSTALL_ROOT/releases/$version"
install -d -m 0755 "$INSTALL_ROOT/releases"
if [ ! -x "$target/bin/esxi-backup" ]; then
  temporary="$INSTALL_ROOT/releases/.${version}.installing"
  rm -rf "$temporary"
  python3 -m venv "$temporary"
  "$temporary/bin/pip" install --disable-pip-version-check --upgrade pip
  source_archive="$temporary/source.tar.gz"
  github_curl -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${REPOSITORY}/tarball/${tag}" -o "$source_archive"
  "$temporary/bin/pip" install --disable-pip-version-check "$source_archive"
  rm -f "$source_archive"
  mv "$temporary" "$target"
fi
ln -sfn "$target" "$INSTALL_ROOT/current.new"
mv -Tf "$INSTALL_ROOT/current.new" "$INSTALL_ROOT/current"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
  "$DATA_DIR" "$DATA_DIR/repository"
install -d -m 0750 "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  cat >"$CONFIG_DIR/config.toml" <<EOF
repository = "$DATA_DIR/repository"
chunk_size_mib = 32
compression_level = 3
pipeline_workers = 2
parallel_disks = 2
quiesce = true

[server]
host = "esxi.example.invalid"
username = "backup-user"
password = "CHANGE-ME"
port = 443
verify_ssl = true
ssh_enabled = false
EOF
  chown root:"$SERVICE_USER" "$CONFIG_DIR/config.toml"
  chmod 0640 "$CONFIG_DIR/config.toml"
fi

github_curl -H 'Accept: application/vnd.github.raw+json' \
  "https://api.github.com/repos/${REPOSITORY}/contents/scripts/install.sh?ref=${tag}" \
  -o /usr/local/sbin/esxi-backup-install
chmod 0755 /usr/local/sbin/esxi-backup-install
if [ -n "$GITHUB_TOKEN" ]; then
  umask 077
  printf 'GITHUB_TOKEN=%s\n' "$GITHUB_TOKEN" >"$CONFIG_DIR/update.env"
fi

cat >/etc/systemd/system/esxi-vm-backup.service <<EOF
[Unit]
Description=ESXi VM Backup Tool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
ExecStart=$INSTALL_ROOT/current/bin/esxi-backup web --config $CONFIG_DIR/config.toml --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/esxi-vm-backup-update.service <<EOF
[Unit]
Description=Update ESXi VM Backup Tool from GitHub Releases
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-$CONFIG_DIR/update.env
ExecStart=/usr/local/sbin/esxi-backup-install --update
EOF

cat >/etc/systemd/system/esxi-vm-backup-update.timer <<'EOF'
[Unit]
Description=Daily ESXi VM Backup Tool update check

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=2h

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now esxi-vm-backup-update.timer
systemctl enable esxi-vm-backup.service
if grep -q 'CHANGE-ME' "$CONFIG_DIR/config.toml"; then
  printf '\nInstalled version %s. Edit %s, then run:\n  systemctl start esxi-vm-backup\n' \
    "$version" "$CONFIG_DIR/config.toml"
else
  systemctl restart esxi-vm-backup.service
  printf 'Installed and started ESXi VM Backup Tool %s.\n' "$version"
fi
