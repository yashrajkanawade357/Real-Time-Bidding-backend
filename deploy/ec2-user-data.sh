#!/bin/bash
# EC2 first-boot script for Ubuntu 24.04. Pass it as "User data" when launching
# the instance: it installs Docker, fetches the app and starts Postgres plus two
# API instances. No SSH needed. Progress: /var/log/cloud-init-output.log
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

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$APP_DIR"
fi

cd "$APP_DIR"
# restart: unless-stopped in the compose file brings everything back after a reboot.
docker compose -f docker-compose.yml -f deploy/docker-compose.aws.yml up -d --build
