#!/usr/bin/env bash
# Run on the VM as a user with sudo: ./deploy/deploy.sh [branch]
set -euo pipefail

BRANCH="${1:-main}"
cd /srv/pm-dashboard

git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

cd backend
.venv/bin/pip install -q .
set -a; source /etc/pm-dashboard/api.env; set +a
.venv/bin/alembic upgrade head

sudo systemctl restart pm-api pm-worker pm-beat
sleep 3
curl -fsS http://127.0.0.1:8000/healthz && echo " deploy ok"
