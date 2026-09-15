#!/usr/bin/env bash
# Install a persistent finn-nancy systemd instance.
#
# Usage:
#   bash deploy/install.sh                 # system prod: /etc/finn-nancy/prod.env
#   bash deploy/install.sh dev             # system dev:  /etc/finn-nancy/dev.env
#   bash deploy/install.sh --user prod     # user prod:   ~/.config/finn-nancy/prod.env
#   bash deploy/install.sh --user --no-start prod
set -euo pipefail

SCOPE="system"
START=1
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
    --no-start)
      START=0
      shift
      ;;
    prod|dev)
      INSTANCE="$1"
      shift
      ;;
    *)
      echo "usage: bash deploy/install.sh [--system|--user] [--no-start] [prod|dev]" >&2
      exit 2
      ;;
  esac
done

if [[ "$SCOPE" != "system" && "$SCOPE" != "user" ]]; then
  echo "invalid scope: $SCOPE" >&2
  exit 2
fi
if [[ "$INSTANCE" != "prod" && "$INSTANCE" != "dev" ]]; then
  echo "usage: bash deploy/install.sh [--system|--user] [--no-start] [prod|dev]" >&2
  exit 2
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="${FINN_NANCY_USER:-$(id -un)}"
APP_GROUP="${FINN_NANCY_GROUP:-$(id -gn)}"
FINANCE_DIR="${FINANCE_DIR:-$HOME/finn-nancy-data}"
if [[ "$SCOPE" == "user" ]]; then
  CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
  ENV_DIR="$CONFIG_HOME/finn-nancy"
  UNIT_DIR="$CONFIG_HOME/systemd/user"
  UNIT_TEMPLATE="$REPO/deploy/finn-nancy-user@.service"
  SYSTEMCTL=(systemctl --user)
else
  ENV_DIR="/etc/finn-nancy"
  UNIT_DIR="/etc/systemd/system"
  UNIT_TEMPLATE="$REPO/deploy/finn-nancy@.service"
  SYSTEMCTL=(sudo systemctl)
fi
ENV_FILE="$ENV_DIR/$INSTANCE.env"

cd "$REPO"
uv sync

tmp_unit="$(mktemp)"
trap 'rm -f "$tmp_unit" "${tmp_env:-}"' EXIT
if [[ "$SCOPE" == "user" ]]; then
  sed \
    -e "s#__REPO__#$REPO#g" \
    -e "s#__ENV_DIR__#$ENV_DIR#g" \
    "$UNIT_TEMPLATE" > "$tmp_unit"
else
  sed \
    -e "s#__REPO__#$REPO#g" \
    -e "s#__USER__#$APP_USER#g" \
    -e "s#__GROUP__#$APP_GROUP#g" \
    "$UNIT_TEMPLATE" > "$tmp_unit"
fi

if [[ "$SCOPE" == "user" ]]; then
  install -d -m 0700 "$ENV_DIR" "$UNIT_DIR"
  install -m 0644 "$tmp_unit" "$UNIT_DIR/finn-nancy@.service"
else
  sudo install -d -m 0755 "$ENV_DIR"
  sudo install -m 0644 "$tmp_unit" "$UNIT_DIR/finn-nancy@.service"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  tmp_env="$(mktemp)"
  if [[ "$INSTANCE" == "prod" && -f "$REPO/.env" ]]; then
    awk -F= '
      /^[[:space:]]*($|#)/ { print; next }
      {
        key=$1
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
        if (key != "DB_PATH" && key != "DATA_DIR" && key != "ADDR" && key != "READ_ONLY") print
      }
    ' "$REPO/.env" > "$tmp_env"
    {
      echo ""
      echo "# deploy/install.sh production overrides"
      echo "DB_PATH=$FINANCE_DIR/finn-nancy.sqlite"
      echo "DATA_DIR=$FINANCE_DIR"
      echo "ADDR=${ADDR:-127.0.0.1:8770}"
      echo "READ_ONLY=false"
    } >> "$tmp_env"
  elif [[ "$INSTANCE" == "prod" ]]; then
    sed \
      -e "s#__DATA_DIR__#$FINANCE_DIR#g" \
      "$REPO/deploy/env/prod.env.example" > "$tmp_env"
  else
    sed \
      -e "s#__REPO__#$REPO#g" \
      "$REPO/deploy/env/dev.env.example" > "$tmp_env"
  fi
  if [[ "$SCOPE" == "user" ]]; then
    install -m 0600 "$tmp_env" "$ENV_FILE"
  else
    sudo install -m 0600 -o root -g root "$tmp_env" "$ENV_FILE"
  fi
  echo "created $ENV_FILE"
else
  echo "kept existing $ENV_FILE"
fi

"${SYSTEMCTL[@]}" daemon-reload

if [[ "$INSTANCE" == "prod" ]]; then
  # Retire the old singleton service if it was installed, otherwise it can run
  # a second worker against the same DB.
  if [[ "$SCOPE" == "system" ]]; then
    sudo systemctl disable --now finn-nancy.service >/dev/null 2>&1 || true
  fi
fi

if [[ "$START" == "1" ]]; then
  "${SYSTEMCTL[@]}" enable --now "finn-nancy@$INSTANCE.service"
  sleep 2
  "${SYSTEMCTL[@]}" --no-pager --lines=12 status "finn-nancy@$INSTANCE.service"
else
  "${SYSTEMCTL[@]}" enable "finn-nancy@$INSTANCE.service"
  echo "installed and enabled finn-nancy@$INSTANCE.service ($SCOPE), not started"
fi
