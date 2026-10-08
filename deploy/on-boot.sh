#!/bin/bash
# Runs on every boot of the EC2 instance - the first boot via ec2-user-data.sh,
# every later boot via the per-boot hook it installs. Makes sure the server's
# secrets exist, pulls the latest code, then builds and (re)starts the stack.
# So "reboot" means "redeploy" (aws-launch.sh update).
set -euo pipefail

APP_DIR=/opt/bidding
cd "$APP_DIR"
git pull --ff-only || echo "git pull failed; starting the code already on this machine"

# Secrets are generated here and never leave this machine: a root-only .env
# next to the compose file. Neither is in the repository or the user data.
umask 077
touch .env
if ! grep -q '^POSTGRES_PASSWORD=' .env; then
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env
fi
if ! grep -q '^ADMIN_KEY=' .env; then
  echo "ADMIN_KEY=$(openssl rand -base64 33 | tr '+/' '-_' | tr -d '=\n')" >> .env
fi

COMPOSE="docker compose -f docker-compose.yml -f deploy/docker-compose.aws.yml"
$COMPOSE up -d --build --remove-orphans
# Recreated API containers can come back on new addresses; nginx resolves at start.
$COMPOSE restart lb

# The admin key goes to the instance's console log, which only principals in this
# AWS account can read (aws-launch.sh admin-key). Printed on every boot so the
# latest log always has it.
echo "BIDDING_ADMIN_KEY=$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)" > /dev/console
