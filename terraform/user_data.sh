#!/bin/bash
set -euo pipefail
exec > /var/log/user_data.log 2>&1

echo "=== Bootstrap started: $(date) ==="

# ── System updates and dependencies ─────────────────────────────────────────
dnf update -y
dnf install -y docker git

# ── Docker ───────────────────────────────────────────────────────────────────
systemctl enable --now docker
usermod -aG docker ec2-user

# ── Docker Compose plugin ────────────────────────────────────────────────────
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL \
  "https://github.com/docker/compose/releases/download/v2.27.1/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# ── Clone application ────────────────────────────────────────────────────────
git clone "${app_repo_url}" /opt/trading212
chown -R ec2-user:ec2-user /opt/trading212

# ── Systemd service — starts the app automatically on boot ──────────────────
cat > /etc/systemd/system/trading212.service <<'EOF'
[Unit]
Description=Trading 212 Dashboard (Docker Compose)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/trading212
ExecStart=/usr/local/lib/docker/cli-plugins/docker-compose \
    -f docker-compose.prod.yml up -d --remove-orphans
ExecStop=/usr/local/lib/docker/cli-plugins/docker-compose \
    -f docker-compose.prod.yml down
User=ec2-user
Group=ec2-user

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable trading212

echo "=== Bootstrap complete: $(date) ==="
echo "Next step: SSH in and create /opt/trading212/.env before starting the service."
