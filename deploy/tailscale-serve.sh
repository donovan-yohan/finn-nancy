#!/usr/bin/env bash
# Publish only through an authenticated private overlay. The application itself
# remains bound to loopback and unauthenticated; do not enable public sharing.
set -euo pipefail

TARGET="${TAILSCALE_TARGET:?Set TAILSCALE_TARGET to a loopback URL, for example http://127.0.0.1:8080}"
case "$TARGET" in
  http://127.0.0.1:*|http://\[::1\]:*) ;;
  *)
    echo "refusing non-loopback TAILSCALE_TARGET" >&2
    exit 2
    ;;
esac

sudo tailscale serve --bg --yes "$TARGET"
sudo tailscale serve status
