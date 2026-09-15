#!/usr/bin/env bash
# Safe update: pull latest code, sync deps, then restart a systemd instance.
#
# Usage:
#   bash deploy/update.sh                 # system prod
#   bash deploy/update.sh dev             # system dev
#   bash deploy/update.sh --user prod     # user prod, no sudo
set -euo pipefail

SCOPE="system"
INSTANCE="prod"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --system)
      SCOPE="system"
      shift
      ;;
    --user)
      SCOPE="user"
      shift
      ;;
    prod|dev)
      INSTANCE="$1"
      shift
      ;;
    *)
      echo "usage: bash deploy/update.sh [--system|--user] [prod|dev]" >&2
      exit 2
      ;;
  esac
done

if [[ "$SCOPE" != "system" && "$SCOPE" != "user" ]]; then
  echo "invalid scope: $SCOPE" >&2
  exit 2
fi
if [[ "$INSTANCE" != "prod" && "$INSTANCE" != "dev" ]]; then
  echo "usage: bash deploy/update.sh [--system|--user] [prod|dev]" >&2
  exit 2
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
if [[ "$SCOPE" == "user" ]]; then
  ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/finn-nancy/$INSTANCE.env"
  SYSTEMCTL=(systemctl --user)
else
  ENV_FILE="/etc/finn-nancy/$INSTANCE.env"
  SYSTEMCTL=(sudo systemctl)
fi
DB_PATH=""
ADDR=""

echo "==> git pull"
git pull --ff-only || echo "   (no upstream / nothing to pull - using local working tree)"

echo "==> uv sync"
uv sync

if [[ -r "$ENV_FILE" ]]; then
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    case "$key" in
      DB_PATH) DB_PATH="$value" ;;
      ADDR) ADDR="$value" ;;
    esac
  done < "$ENV_FILE"
  if [[ -n "${DB_PATH:-}" ]]; then
    echo "==> migrate $DB_PATH"
    uv run fn migrate --db "$DB_PATH"
  fi
else
  echo "==> env file not readable ($ENV_FILE); relying on service ExecStartPre migration"
fi

echo "==> restart finn-nancy@$INSTANCE.service ($SCOPE)"
"${SYSTEMCTL[@]}" restart "finn-nancy@$INSTANCE.service"
sleep 2
"${SYSTEMCTL[@]}" --no-pager --lines=8 status "finn-nancy@$INSTANCE.service"

if [[ -n "${ADDR:-}" ]]; then
  echo "==> health http://$ADDR/healthz"
  curl -fsS "http://$ADDR/healthz"
fi
