#!/bin/bash
# EC2 first-boot script for Ubuntu 24.04, passed as the instance's user data by
# aws-launch.sh. Installs Docker, fetches the app, installs a per-boot hook and
# runs deploy/on-boot.sh. No SSH needed. Progress: /var/log/cloud-init-output.log
set -euxo pipefail

REPO=https://github.com/yashrajkanawade357/Real-Time-Bidding-backend.git
APP_DIR=/opt/bidding

# A t3.micro has 1 GB of RAM; 1 GB of swap gives the image build headroom.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Docker Engine + Compose plugin from Docker's official repository.
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --depth 1 "$REPO" "$APP_DIR"
fi

# Every later boot runs the same script: pull, rebuild, restart.
mkdir -p /var/lib/cloud/scripts/per-boot
cat > /var/lib/cloud/scripts/per-boot/bidding.sh <<'SH'
#!/bin/bash
exec bash /opt/bidding/deploy/on-boot.sh
SH
chmod +x /var/lib/cloud/scripts/per-boot/bidding.sh

bash "$APP_DIR/deploy/on-boot.sh"
