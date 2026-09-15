from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest

from app.db.migrate import init_db
from scripts.backup_manifest_lib import (
    FORMAT,
    ManifestError,
    RELEASE_MANIFEST_MAX_AGE,
    verify_manifest,
)

REPO = Path(__file__).resolve().parents[1]


def _load_release_audit() -> ModuleType:
    name = "finn_nancy_release_audit"
    loader = importlib.machinery.SourceFileLoader(
        name, str(REPO / "scripts" / "release-audit")
    )
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


RELEASE_AUDIT = _load_release_audit()


def _dev_env(**updates: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "FN_DEV_DB_PATH",
        "FN_DEV_DATA_DIR",
        "FN_DEV_ADDR",
        "FN_DEV_ALLOW_NON_LOOPBACK",
    ):
        env.pop(key, None)
    env.update(updates)
    return env


def _validate_dev(**env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REPO / "scripts" / "dev"), "--validate-only"],
        cwd=REPO,
        env=_dev_env(**env),
        text=True,
        capture_output=True,
        check=False,
    )


def test_dev_defaults_are_contained_and_loopback():
    result = _validate_dev()
    assert result.returncode == 0, result.stderr
    assert f"db={REPO}/data/local.sqlite" in result.stdout
    assert f"data={REPO}/data" in result.stdout
    assert "addr=127.0.0.1:8771" in result.stdout


def test_dev_rejects_external_data_and_implicit_network_exposure(tmp_path):
    outside = _validate_dev(FN_DEV_DATA_DIR=str(tmp_path))
    assert outside.returncode == 2
    assert "outside" in outside.stderr

    exposed = _validate_dev(FN_DEV_ADDR="192.0.2.10:8771")
    assert exposed.returncode == 2
    assert "refusing non-loopback" in exposed.stderr

    explicit = _validate_dev(
        FN_DEV_ADDR="192.0.2.10:8771",
        FN_DEV_ALLOW_NON_LOOPBACK="1",
    )
    assert explicit.returncode == 0, explicit.stderr
    assert "EXPLICIT NON-LOOPBACK DEV BIND ENABLED" in explicit.stderr


@pytest.mark.parametrize("target", ("http://127.0.0.1:8080", "http://[::1]:8080"))
def test_tailscale_serve_accepts_only_valid_loopback_targets(target, tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_sudo.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["TAILSCALE_TARGET"] = target

    result = subprocess.run(
        [str(REPO / "deploy" / "tailscale-serve.sh")],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "target",
    (
        "http://" + "1:8080",
        "http://::8080",
        "http://192.0.2.10:8080",
    ),
)
def test_tailscale_serve_rejects_non_loopback_or_malformed_targets(target, tmp_path):
    env = os.environ.copy()
    env["TAILSCALE_TARGET"] = target

    result = subprocess.run(
        [str(REPO / "deploy" / "tailscale-serve.sh")],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "refusing non-loopback" in result.stderr


def test_backup_manifest_captures_wal_and_rejects_tampering(tmp_path):
    source = tmp_path / "source.sqlite"
    backup = tmp_path / "release-backup.sqlite"
    manifest = tmp_path / "release-backup.manifest.json"
    init_db(source)

    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE synthetic_wal_probe(value TEXT NOT NULL)")
        writer.execute("INSERT INTO synthetic_wal_probe(value) VALUES ('synthetic')")
        writer.commit()

        create = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "backup-manifest"),
                "create",
                "--source-db",
                str(source),
                "--backup",
                str(backup),
                "--manifest",
                str(manifest),
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
        assert create.returncode == 0, create.stderr
    finally:
        writer.close()

    payload = json.loads(manifest.read_text())
    assert payload["format"] == "finn-nancy-backup-manifest-v1"
    source_identity = payload["source"]["database_identity"]
    assert str(uuid.UUID(source_identity)) == source_identity
    assert payload["candidate"]["database_identity"] == source_identity
    assert len(payload["source"]["schema_digest"]) == 64
    assert len(payload["source"]["migration_digest"]) == 64
    assert len(payload["backup"]["sha256"]) == 64
    assert str(source) not in manifest.read_text()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM synthetic_wal_probe").fetchone()[0] == 1
        from app.db.migrate import read_database_identity

        assert read_database_identity(conn) == source_identity

    verify = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "backup-manifest"),
            "verify",
            "--manifest",
            str(manifest),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr

    with backup.open("ab") as handle:
        handle.write(b"tamper")
    rejected = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "backup-manifest"),
            "verify",
            "--manifest",
            str(manifest),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert "SHA-256 mismatch" in rejected.stderr


def test_release_manifest_freshness_is_bounded(tmp_path):
    now = dt.datetime(2026, 7, 26, 12, 0, tzinfo=dt.UTC)
    stale = now - RELEASE_MANIFEST_MAX_AGE - dt.timedelta(seconds=1)
    manifest = tmp_path / "stale.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "format": FORMAT,
                "created_at": stale.isoformat().replace("+00:00", "Z"),
            }
        )
    )

    with pytest.raises(ManifestError, match="stale for release audit"):
        verify_manifest(
            manifest,
            max_age=RELEASE_MANIFEST_MAX_AGE,
            now=now,
        )
    assert RELEASE_AUDIT.RELEASE_MANIFEST_MAX_AGE == dt.timedelta(hours=24)


def test_release_audit_skips_template_only_unit_names(monkeypatch):
    outputs = {
        "list-units": (
            "finn-nancy@prod.service loaded active running production\n"
            "finn-nancy@staging.service loaded inactive dead staging\n"
        ),
        "list-unit-files": (
            "finn-nancy.service disabled enabled\n"
            "finn-nancy@.service indirect enabled\n"
            "finn-nancy@prod.service enabled enabled\n"
        ),
    }

    def fake_systemctl(
        scope: str, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        assert scope == "system"
        assert check
        return subprocess.CompletedProcess(
            ["systemctl", *args],
            0,
            outputs[args[0]],
            "",
        )

    monkeypatch.setattr(RELEASE_AUDIT, "_systemctl", fake_systemctl)

    assert RELEASE_AUDIT._unit_names(
        "system", "finn-nancy@prod.service"
    ) == {
        "finn-nancy.service",
        "finn-nancy@prod.service",
        "finn-nancy@staging.service",
    }


def _write_process(
    proc_root: Path,
    pid: int,
    ppid: int,
    args: tuple[str, ...],
    cgroup: str,
) -> None:
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir(parents=True)
    (proc_dir / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    (proc_dir / "status").write_text(f"Name:\ttest\nPPid:\t{ppid}\n")
    (proc_dir / "cgroup").write_text(f"0::{cgroup}\n")


def test_release_audit_rejects_unmanaged_process_and_listener(
    tmp_path, monkeypatch
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    control_group = "/user.slice/finn-nancy@prod.service"
    _write_process(proc_root, 100, 1, ("uv", "run", "fn", "serve"), control_group)
    _write_process(
        proc_root,
        101,
        100,
        ("python", "-m", "uvicorn", "app.web.app:create_app"),
        control_group,
    )
    selected = {"MainPID": "100", "ControlGroup": control_group}

    main_pid, actual_cgroup, processes = RELEASE_AUDIT._validate_process_owner(
        selected, proc_root
    )
    assert main_pid == 100
    assert actual_cgroup == control_group
    assert [process.pid for process in processes] == [100, 101]

    monkeypatch.setattr(
        RELEASE_AUDIT,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command,
            0,
            'LISTEN 0 2048 127.0.0.1:8770 192.0.2.10:* users:(("python",pid=101,fd=7))\n',
            "",
        ),
    )
    RELEASE_AUDIT._validate_listener_owner(
        "http://127.0.0.1:8770", main_pid, control_group, proc_root
    )

    _write_process(proc_root, 200, 1, ("fn", "serve"), "/user.slice/rogue.service")
    with pytest.raises(RELEASE_AUDIT.AuditError, match="unmanaged"):
        RELEASE_AUDIT._validate_process_owner(selected, proc_root)

    monkeypatch.setattr(
        RELEASE_AUDIT,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command,
            0,
            'LISTEN 0 2048 127.0.0.1:8770 192.0.2.10:* users:(("python",pid=200,fd=7))\n',
            "",
        ),
    )
    with pytest.raises(RELEASE_AUDIT.AuditError, match="outside"):
        RELEASE_AUDIT._validate_listener_owner(
            "http://127.0.0.1:8770", main_pid, control_group, proc_root
        )


def test_release_audit_requires_origin_only_loopback_url():
    assert RELEASE_AUDIT._parse_local_base_url("http://127.0.0.1:8770") == (
        "127.0.0.1",
        8770,
    )
    with pytest.raises(RELEASE_AUDIT.AuditError, match="loopback"):
        RELEASE_AUDIT._parse_local_base_url("http://192.0.2.10:8770")
    with pytest.raises(RELEASE_AUDIT.AuditError, match="origin-only"):
        RELEASE_AUDIT._parse_local_base_url("http://127.0.0.1:8770/version")


@pytest.mark.parametrize(
    "local_address",
    (
        "0.0.0.0:8770",
        "[::]:8770",
        "192.0.2.10:8770",
        "[2001:db8::10]:8770",
    ),
)
def test_release_audit_rejects_wildcard_and_non_loopback_listeners(
    local_address, tmp_path, monkeypatch
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    control_group = "/user.slice/finn-nancy@prod.service"
    _write_process(proc_root, 101, 1, ("fn", "serve"), control_group)
    monkeypatch.setattr(
        RELEASE_AUDIT,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command,
            0,
            (
                f"LISTEN 0 2048 {local_address} *:* "
                'users:(("python",pid=101,fd=7))\n'
            ),
            "",
        ),
    )

    with pytest.raises(RELEASE_AUDIT.AuditError, match="wildcard|non-loopback"):
        RELEASE_AUDIT._validate_listener_owner(
            "http://127.0.0.1:8770", 101, control_group, proc_root
        )


def test_release_audit_accepts_only_loopback_listener_rows(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    control_group = "/user.slice/finn-nancy@prod.service"
    _write_process(proc_root, 101, 1, ("fn", "serve"), control_group)
    monkeypatch.setattr(
        RELEASE_AUDIT,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command,
            0,
            (
                'LISTEN 0 2048 127.0.0.1:8770 192.0.2.10:* users:(("python",pid=101,fd=7))\n'
                'LISTEN 0 2048 [::1]:8770 [::]:* users:(("python",pid=101,fd=8))\n'
            ),
            "",
        ),
    )

    RELEASE_AUDIT._validate_listener_owner(
        "http://127.0.0.1:8770", 101, control_group, proc_root
    )
