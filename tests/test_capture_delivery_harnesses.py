from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_soak_writes_content_free_manifest(tmp_path):
    manifest = tmp_path / "soak.json"
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "soak-capture"),
            "--count",
            "10",
            "--allow-small",
            "--output",
            str(manifest),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(manifest.read_text())
    assert body["accepted"] == 10
    assert body["durable"] == 10
    assert body["loss"] == 0
    assert body["point_durability_pct"] == 100.0
    assert body["status"] == "passed"
    assert body["fresh_process_verified"] is True
    assert body["verified_originals"] == 10
    assert body["containment_failures"] == 0
    assert body["missing_originals"] == 0
    assert body["sha256_failures"] == 0
    assert body["content_free"] is True
    assert body["physical_phone_proven"] is False
    forbidden = {
        "capture_id",
        "filename",
        "path",
        "merchant",
        "account",
        "receipt_text",
        "token",
    }
    assert forbidden.isdisjoint(body)


@pytest.mark.parametrize(
    ("mutation", "failure_key"),
    (("delete", "missing_originals"), ("corrupt", "sha256_failures")),
)
def test_synthetic_soak_rejects_missing_or_changed_original(
    tmp_path, mutation, failure_key
):
    manifest = tmp_path / f"soak-{mutation}.json"
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "soak-capture"),
            "--count",
            "3",
            "--allow-small",
            "--mutation",
            mutation,
            "--output",
            str(manifest),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    body = json.loads(manifest.read_text())
    assert body["status"] == "failed"
    assert body["fresh_process_verified"] is True
    assert body[failure_key] == 1
    assert body["verified_originals"] == 2


def test_phone_harness_records_no_device_blocker_without_private_ids(tmp_path):
    fake_adb = tmp_path / "adb"
    fake_adb.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"devices\" ]]; then\n"
        "  printf 'List of devices attached\\n\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_adb.chmod(0o755)
    manifest = tmp_path / "phone.json"
    env = os.environ.copy()
    env["ADB_BIN"] = str(fake_adb)
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "dogfood-capture-phone"),
            "--output",
            str(manifest),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 3
    body = json.loads(manifest.read_text())
    assert body["status"] == "blocked"
    assert body["blocker"] == "one_authorized_adb_device_required"
    assert body["content_free"] is True
    assert "device_serial" not in body


def test_phone_harness_rejects_stale_served_identity_before_device_capture(
    tmp_path,
):
    served = tmp_path / "served"
    served.mkdir()
    database_identity = str(uuid4())
    (served / "version").write_text(
        json.dumps(
            {
                "build_sha": "0" * 40,
                "build_tree_sha": "1" * 40,
                "database_identity": database_identity,
                "schema_digest": "2" * 64,
                "expected_migration_head": "032_capture_telemetry.sql",
                "applied_migration_head": "032_capture_telemetry.sql",
                "expected_migration_digest": "3" * 64,
                "applied_migration_digest": "3" * 64,
                "migration_sequence_matches": True,
            }
        )
    )
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        [
            "python3",
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(served),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marker = tmp_path / "device-shell-called"
    fake_adb = tmp_path / "adb"
    fake_adb.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"devices\" ]]; then\n"
        "  printf 'List of devices attached\\nsynthetic\\tdevice\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == \"get-serialno\" ]]; then\n"
        "  printf 'synthetic\\n'\n"
        "  exit 0\n"
        "fi\n"
        f"touch {marker!s}\n"
        "exit 1\n"
    )
    fake_adb.chmod(0o755)
    manifest = tmp_path / "phone-stale.json"
    env = os.environ.copy()
    env["ADB_BIN"] = str(fake_adb)
    try:
        for _ in range(50):
            if server.poll() is not None:
                raise AssertionError("fixture HTTP server exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        result = subprocess.run(
            [
                str(ROOT / "scripts" / "dogfood-capture-phone"),
                "--base-url",
                f"http://127.0.0.1:{port}",
                "--proof-session",
                str(uuid4()),
                "--expected-database-identity",
                database_identity,
                "--output",
                str(manifest),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        server.terminate()
        server.wait(timeout=5)

    assert result.returncode == 3
    body = json.loads(manifest.read_text())
    assert body["blocker"] == "served_head_mismatch"
    assert marker.exists() is False


def test_phone_harness_rejects_absent_migration_identity_before_device_capture(
    tmp_path,
):
    served = tmp_path / "served"
    served.mkdir()
    database_identity = str(uuid4())
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (served / "version").write_text(
        json.dumps(
            {
                "build_sha": head,
                "build_tree_sha": tree,
                "database_identity": database_identity,
                "schema_digest": "2" * 64,
                "migration_status": "ok",
                "migration_sequence_matches": True,
            }
        )
    )
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        [
            "python3",
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(served),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marker = tmp_path / "device-command-called"
    fake_adb = tmp_path / "adb"
    fake_adb.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"devices\" ]]; then\n"
        "  printf 'List of devices attached\\nsynthetic\\tdevice\\n'\n"
        "  exit 0\n"
        "fi\n"
        f"touch {marker!s}\n"
        "exit 1\n"
    )
    fake_adb.chmod(0o755)
    manifest = tmp_path / "phone-missing-migration-identity.json"
    env = os.environ.copy()
    env["ADB_BIN"] = str(fake_adb)
    try:
        for _ in range(50):
            if server.poll() is not None:
                raise AssertionError("fixture HTTP server exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        result = subprocess.run(
            [
                str(ROOT / "scripts" / "dogfood-capture-phone"),
                "--base-url",
                f"http://127.0.0.1:{port}",
                "--proof-session",
                str(uuid4()),
                "--expected-database-identity",
                database_identity,
                "--output",
                str(manifest),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        server.terminate()
        server.wait(timeout=5)

    assert result.returncode == 3
    body = json.loads(manifest.read_text())
    assert body["blocker"] == "served_migration_identity_invalid"
    assert marker.exists() is False
