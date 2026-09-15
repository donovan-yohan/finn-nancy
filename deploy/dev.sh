#!/usr/bin/env bash
# Foreground dev server on a non-production port. Intended for iteration.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

export DB_PATH="${DB_PATH:-$REPO/data/local.sqlite}"
export DATA_DIR="${DATA_DIR:-$REPO/data}"
export ADDR="${ADDR:-127.0.0.1:8771}"

uv sync
uv run fn init-db --db "$DB_PATH"
exec uv run fn serve --db "$DB_PATH" --addr "$ADDR"
