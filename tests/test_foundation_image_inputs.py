"""Immutable image contexts must carry the reviewed UI foundation and license."""
from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_INPUTS = (
    "app/web/static/foundation/SOURCE.json",
    "app/web/static/foundation/tokens.json",
    "app/web/static/foundation/vendor/LICENSE.bootstrap",
)


def generated_repository(path: Path, extra: tuple[str, ...]) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    files = (
        "Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
        "THIRD_PARTY_NOTICES.md", "app/config.py", "migrations/001_tables.sql",
        "migrations/002_views.sql", "deploy/container_entrypoint.py", *extra,
    )
    for name in files:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated committed input\n")
    subprocess.run(["git", "-C", str(path), "add", "--", *files], check=True)
    subprocess.run([
        "git", "-C", str(path), "-c", "user.name=Generated Fixture",
        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-qm", "generated context",
    ], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_context_carries_foundation_metadata_and_verbatim_license(tmp_path):
    repo = tmp_path / "generated-repository"
    revision = generated_repository(repo, FOUNDATION_INPUTS)
    output = tmp_path / "context.tar"
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/build-image"), "--repository", str(repo),
        "--revision", revision, "--export-context", str(output),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    with tarfile.open(output) as archive:
        for name in FOUNDATION_INPUTS:
            assert name in receipt["files"]
            handle = archive.extractfile(name)
            assert handle is not None
            assert handle.read() == (repo / name).read_bytes()


@pytest.mark.parametrize("name", [
    "app/web/static/unreviewed.json",
    "app/web/static/foundation/unreviewed.json",
    "app/web/static/foundation/vendor/LICENSE.unreviewed",
    "app/web/static/foundation/private/generated.py",
])
def test_foundation_support_does_not_allow_arbitrary_metadata_or_private_paths(tmp_path, name):
    repo = tmp_path / "generated-repository"
    revision = generated_repository(repo, (name,))
    output = tmp_path / "context.tar"
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/build-image"), "--repository", str(repo),
        "--revision", revision, "--export-context", str(output),
    ], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert not output.exists()
