"""PWA manifest and root-scoped service worker routes."""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
SW_CACHE_VERSION_PLACEHOLDER = "__CACHE_VERSION__"


def _read_static(name: str) -> bytes:
    return (STATIC_DIR / name).read_bytes()


def static_assets_version(static_dir: Path) -> str:
    hasher = hashlib.sha256()
    for asset_path in sorted(static_dir.iterdir(), key=lambda path: path.name):
        if asset_path.name == "sw.js" or not asset_path.is_file():
            continue
        hasher.update(asset_path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(asset_path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()[:12]


@router.get("/manifest.webmanifest")
def manifest() -> Response:
    return Response(
        content=_read_static("manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@router.get("/sw.js")
def service_worker() -> Response:
    version = static_assets_version(STATIC_DIR)
    content = (STATIC_DIR / "sw.js").read_text(encoding="utf-8").replace(
        SW_CACHE_VERSION_PLACEHOLDER,
        version,
    )
    return Response(
        content=content,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )
