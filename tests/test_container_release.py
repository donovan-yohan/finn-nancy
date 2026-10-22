"""Generated-only container admission and immutable build regressions."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import migrate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    path = ROOT / "deploy/container_entrypoint.py"
    assert path.is_file(), "container admission launcher is missing"
    spec = importlib.util.spec_from_file_location("container_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)  # Never load a checkout dotenv.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADDR", "0.0.0.0:8080")
    get_settings.cache_clear()
    yield module
    get_settings.cache_clear()


def test_admits_existing_migrated_state_then_execs_only_serve(launcher, monkeypatch, tmp_path):
    db = tmp_path / "generated ?#ledger.sqlite"
    migrate.init_db(db)
    before = db.read_bytes()
    monkeypatch.setenv("DB_PATH", str(db))
    calls = []
    monkeypatch.setattr(launcher.os, "execv", lambda *args: calls.append(args))
    assert launcher.main([]) == 0
    assert calls == [(str(Path(launcher.sys.executable).parent / "fn"), [
        "fn", "serve", "--db", str(db), "--addr", "0.0.0.0:8080",
    ])]
    assert db.read_bytes() == before


@pytest.mark.parametrize("kind", ["outside", "db_symlink", "parent_symlink", "data_symlink", "relative_db", "relative_data"])
def test_state_paths_cannot_escape_persistent_root(launcher, monkeypatch, tmp_path, kind):
    data = tmp_path / "state"
    data.mkdir()
    db = data / "ledger.sqlite"
    migrate.init_db(db)
    if kind == "outside":
        db = tmp_path / "outside.sqlite"
        migrate.init_db(db)
    elif kind == "db_symlink":
        alias = data / "alias.sqlite"
        alias.symlink_to(db)
        db = alias
    elif kind == "parent_symlink":
        alias = data / "alias"
        alias.symlink_to(data, target_is_directory=True)
        db = alias / "ledger.sqlite"
    elif kind == "data_symlink":
        alias = tmp_path / "state-alias"
        alias.symlink_to(data, target_is_directory=True)
        data = alias
    elif kind == "relative_db":
        db = Path("state/ledger.sqlite")
    else:
        data = Path("state")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setattr(launcher.os, "execv", lambda *_: pytest.fail("must not serve"))
    assert launcher.main([]) == 1


def test_runtime_arguments_refused_before_settings(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "get_settings", lambda: pytest.fail("arguments must fail first"))
    assert launcher.main(["init-db"]) == 1
    assert capsys.readouterr().err == "container startup refused: arguments are not supported\n"


@pytest.mark.parametrize("packaging", ["empty", "missing_baseline", "empty_sql"])
def test_missing_packaged_migrations_refused(launcher, monkeypatch, tmp_path, packaging):
    db = tmp_path / "ledger.sqlite"
    migrate.init_db(db)
    monkeypatch.setenv("DB_PATH", str(db))
    packaged = tmp_path / "migrations"
    packaged.mkdir()
    if packaging == "missing_baseline":
        (packaged / "003_only.sql").write_text("SELECT 1;")
    elif packaging == "empty_sql":
        (packaged / "001_tables.sql").touch()
        (packaged / "002_views.sql").write_text("SELECT 1;")
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", packaged)
    monkeypatch.setattr(launcher.os, "execv", lambda *_: pytest.fail("must not serve"))
    assert launcher.main([]) == 1


@pytest.mark.parametrize("kind", ["absent", "blank", "corrupt"])
def test_invalid_database_refused_without_creation_or_changes(launcher, monkeypatch, tmp_path, capsys, kind):
    db = tmp_path / "private-generated.sqlite"
    if kind != "absent":
        db.write_bytes(b"" if kind == "blank" else b"not sqlite")
    before = db.read_bytes() if db.exists() else None
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setattr(launcher.os, "execv", lambda *_: pytest.fail("must not serve"))
    assert launcher.main([]) == 1
    assert (db.read_bytes() if db.exists() else None) == before
    assert capsys.readouterr().err == "container startup refused: invalid persistent state\n"


@pytest.mark.parametrize("damage", ["missing", "unknown", "reordered", "identity", "no_identity"])
def test_requires_exact_ordered_ledger_and_valid_identity(launcher, monkeypatch, tmp_path, damage):
    db = tmp_path / "generated.sqlite"
    migrate.init_db(db)
    with sqlite3.connect(db) as conn:
        if damage == "missing":
            conn.execute("DELETE FROM schema_migrations WHERE filename='002_views.sql'")
        elif damage == "unknown":
            conn.execute("INSERT INTO schema_migrations(filename) VALUES ('999_unknown.sql')")
        elif damage == "reordered":
            conn.execute("DELETE FROM schema_migrations WHERE filename='001_tables.sql'")
            conn.execute("INSERT INTO schema_migrations(filename) VALUES ('001_tables.sql')")
        elif damage == "identity":
            conn.execute("UPDATE app_settings SET value='invalid' WHERE key=?", (migrate.DATABASE_IDENTITY_SETTING_KEY,))
        else:
            conn.execute("DELETE FROM app_settings WHERE key=?", (migrate.DATABASE_IDENTITY_SETTING_KEY,))
    before = db.read_bytes()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setattr(launcher.os, "execv", lambda *_: pytest.fail("must not serve"))
    assert launcher.main([]) == 1
    assert db.read_bytes() == before


def test_build_exports_only_clean_committed_allowlist(tmp_path):
    script = ROOT / "scripts/build-image"
    assert script.is_file(), "immutable image build script is missing"
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    files = ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
             "THIRD_PARTY_NOTICES.md", "app/config.py", "migrations/001_tables.sql",
             "migrations/002_views.sql", "deploy/container_entrypoint.py"]
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated synthetic input\n")
    (repo / ".gitignore").write_text(".env\ndata/\n")
    (repo / ".env").write_text("synthetic dotenv must not be archived\n")
    (repo / "data").mkdir()
    (repo / "data/private.sqlite").write_text("synthetic excluded state\n")
    git("add", "--", *files, ".gitignore")
    git("-c", "user.name=Generated Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "generated")
    revision = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    output = tmp_path / "context.tar"
    command = [sys.executable, str(script), "--repository", str(repo), "--revision", revision,
               "--export-context", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert (receipt["revision"], receipt["tree"]) == (revision, tree)
    with tarfile.open(output) as archive:
        names = {member.name for member in archive if member.isfile()}
    assert names == set(files)
    (repo / "app/config.py").write_text("uncommitted replacement\n")
    output.unlink()
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()
    assert "uncommitted" in result.stderr


@pytest.mark.parametrize("attribute", ["export-subst", "export-ignore"])
def test_build_context_identity_ignores_mutable_archive_attributes(tmp_path, attribute):
    repo = tmp_path / "generated-repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])

    git("init", "-q")
    files = ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
             "THIRD_PARTY_NOTICES.md", "app/config.py", "app/consumer.py",
             "migrations/001_tables.sql", "migrations/002_views.sql", "deploy/container_entrypoint.py"]
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("GENERATED = '$Format:%H$'\n" if name == "app/config.py" else "generated input\n")
    (repo / "app/consumer.py").chmod(0o755)
    git("add", "--", *files)
    git("-c", "user.name=Generated Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "generated attribute fixture")
    revision = git("rev-parse", "HEAD").decode().strip()
    tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    expected = {}
    for entry in git("ls-tree", "-rz", revision).split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            assert kind == b"blob"
            expected[name.decode()] = (int(mode, 8) & 0o777, git("cat-file", "blob", oid.decode()))

    def export(label):
        output = tmp_path / (label + ".tar")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/build-image"), "--repository", str(repo),
                                 "--revision", revision, "--export-context", str(output)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert (receipt["revision"], receipt["tree"]) == (revision, tree)
        with tarfile.open(output) as archive:
            members = archive.getmembers()
            contents = {m.name: (m.mode, archive.extractfile(m).read()) for m in members if m.isfile()}
        assert len({m.name for m in members}) == len(members)
        directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != "."}
        assert {m.name.rstrip("/") for m in members if m.isdir()} == directories
        assert all(m.isdir() or m.isfile() for m in members)
        return output.read_bytes(), receipt, contents

    _, _, before = export("before")
    assert {name: value[1] for name, value in before.items()} == {name: value[1] for name, value in expected.items()}
    target = "app/config.py" if attribute == "export-subst" else "app/consumer.py"
    (repo / ".git/info/attributes").write_text(f"{target} {attribute}\n")
    assert git("status", "--porcelain", "--untracked-files=all") == b""
    raw, receipt, after = export("after")
    assert set(after) == set(expected), "archive attributes omitted committed inputs"
    assert {name: value[1] for name, value in after.items()} == {name: value[1] for name, value in expected.items()}, "archive attributes rewrote committed bytes"
    assert after == expected, "archive modes must match the selected tree"
    assert receipt["files"] == sorted(expected)
    assert export("repeat")[0] == raw, "identical tree must produce deterministic context bytes"


def test_image_recipe_has_pinned_nonroot_serve_only_inputs():
    recipe = ROOT / "Dockerfile"
    assert recipe.is_file(), "container recipe is missing"
    text = recipe.read_text()
    assert "python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf" in text
    assert "ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d" in text
    assert "USER 10001:10001" in text
    assert "--frozen --no-dev --no-editable" in text
    assert "COPY . ." not in text
    assert 'ENTRYPOINT ["/opt/finn-nancy/.venv/bin/python", "/opt/finn-nancy/deploy/container_entrypoint.py"]' in text
    assert "PLAID_ENABLED=false" in text
    assert "STRICT_LOCAL_MODE=true" in text
    assert "API_TOKEN=" not in text
    assert (ROOT / ".dockerignore").read_text().startswith("# Deny by default")


def test_smoke_interface_never_accepts_caller_database(tmp_path):
    script = ROOT / "scripts/smoke-image"
    assert script.is_file(), "generated-state image smoke is missing"
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--image" in result.stdout
    assert "--db" not in result.stdout
    assert "--state" not in result.stdout
    result = subprocess.run([sys.executable, str(script), "--image", "unused", "--db", str(tmp_path / "never.sqlite")], capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "never.sqlite").exists()


def test_cleanup_inspects_container_type_not_same_named_volume():
    import runpy
    module = runpy.run_path(str(ROOT / "scripts/smoke-image"))
    assert "remove_container" in module, "container cleanup must use typed inspection"
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[:2] == ("container", "inspect") else 0,
                                           stdout="[]\n", stderr="Error response from daemon: No such container: generated-same-name\n")
    assert module["remove_container"](docker, "generated-same-name") is True
    assert calls == [("rm", "-f", "generated-same-name"), ("container", "inspect", "generated-same-name")]


@pytest.mark.parametrize("fault", ["dual_timeout", "removal_timeout", "inspection_timeout", "daemon_error",
                                  "ambiguous_volume", "ambiguous_start", "interrupt"])
def test_smoke_failure_cleanup_and_receipt_survive_transport_errors(monkeypatch, tmp_path, fault):
    import runpy
    module = runpy.run_path(str(ROOT / "scripts/smoke-image"))
    output = tmp_path / "smoke-artifacts"
    monkeypatch.setattr(sys, "argv", ["smoke-image", "--image", "generated-image", "--artifacts", str(output)])
    calls, created_containers, created_volumes, injected = [], [], [], []
    active_containers, active_volumes = set(), set()

    def run(argv, **kwargs):
        assert argv[0] == "docker"
        calls.append(argv)
        command = argv[1:]
        stdout, stderr, status = "", "", 0
        if command[:2] == ["image", "inspect"]:
            stdout = json.dumps([{"Id": "generated-image-id", "Architecture": "arm64", "Os": "linux",
                                  "Config": {"User": "10001:10001", "Labels": {}}}])
        elif command[:2] == ["volume", "create"]:
            created_volumes.append(command[-1])
            active_volumes.add(command[-1])  # Positive creation, before ambiguous response.
            if fault == "ambiguous_volume":
                injected.append("volume create")
                raise subprocess.TimeoutExpired(argv, 120)
        elif command[0] == "run":
            name = command[command.index("--name") + 1]
            created_containers.append(name)
            active_containers.add(name)
            if "--rm" in command:
                active_containers.remove(name)
                stdout = json.dumps({"prepared": "valid"} if command[-1] == "valid" and "os.chown" in command[-2] else {"version": {}})
            elif fault == "ambiguous_start":
                injected.append("run")
                raise subprocess.TimeoutExpired(argv, 120)
        elif command[0] == "exec":
            injected.append("exec")
            if fault == "interrupt":
                raise KeyboardInterrupt()
            raise subprocess.TimeoutExpired(argv, 120)
        elif command[0] == "logs":
            if fault == "dual_timeout":
                injected.append("logs")
                raise subprocess.TimeoutExpired(argv, 120)
        elif command[0] == "rm":
            if fault == "removal_timeout" and "rm" not in injected:
                injected.append("rm")
                raise subprocess.TimeoutExpired(argv, 120)
            active_containers.discard(command[-1])
        elif command[:2] == ["volume", "rm"]:
            active_volumes.discard(command[-1])
        elif command[:2] in (["container", "inspect"], ["volume", "inspect"]):
            if fault == "inspection_timeout" and "inspect" not in injected:
                injected.append("inspect")
                raise subprocess.TimeoutExpired(argv, 120)
            if fault == "daemon_error":
                status, stderr = 1, "Error response from daemon: permission denied"
            else:
                name = command[-1]
                present = name in (active_containers if command[0] == "container" else active_volumes)
                status = 0 if present else 1
                stderr = "" if present else (f"Error response from daemon: No such container: {name}\n" if command[0] == "container" else f"Error response from daemon: get {name}: no such volume\n")
        else:
            pytest.fail(f"unexpected generated transport command: {command[0]}")
        return subprocess.CompletedProcess(argv, status, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", run)
    result = None
    try:
        result = module["main"]()
    except BaseException as exc:
        pytest.fail(f"smoke lost cleanup/receipt to {type(exc).__name__}")
    assert result == 1
    receipt = json.loads((output / "receipt.json").read_text())
    assert created_volumes, "must positively create resources before fault injection"
    assert injected
    if fault not in {"ambiguous_volume", "ambiguous_start"}:
        assert created_containers and injected[0] == "exec"
    if fault == "dual_timeout":
        assert "logs" in injected
    assert receipt["success"] is False
    assert ("KeyboardInterrupt" if fault == "interrupt" else "TimeoutExpired") in receipt["error"]
    assert [c[-1] for c in calls if c[1:2] == ["rm"]] == created_containers
    assert [c[-1] for c in calls if c[1:3] == ["volume", "rm"]] == created_volumes
    assert [c[-1] for c in calls if c[1:3] == ["container", "inspect"]] == created_containers
    assert [c[-1] for c in calls if c[1:3] == ["volume", "inspect"]] == created_volumes
    uncertain = fault in {"dual_timeout", "removal_timeout", "inspection_timeout", "daemon_error"}
    assert receipt["cleanup_verified"] is (not uncertain)
    assert bool(receipt["cleanup_errors"]) is uncertain
    assert receipt["commands"] == calls


@pytest.mark.parametrize("address", ["missing-port", "0.0.0.0:not-a-port", "0.0.0.0:0", "0.0.0.0:65536"])
def test_invalid_bind_refused_before_web_import(launcher, monkeypatch, tmp_path, address, capsys):
    db = tmp_path / "ledger.sqlite"
    migrate.init_db(db)
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("ADDR", address)
    monkeypatch.setattr(launcher.os, "execv", lambda *_: pytest.fail("must not serve"))
    assert launcher.main([]) == 1
    assert address not in capsys.readouterr().err


@pytest.mark.parametrize("target", ["database", "data", "database_parent"])
def test_unwritable_state_refused(launcher, monkeypatch, tmp_path, target):
    nested = tmp_path / "nested"
    nested.mkdir()
    db = nested / "ledger.sqlite"
    migrate.init_db(db)
    monkeypatch.setenv("DB_PATH", str(db))
    denied = {"database": db, "data": tmp_path, "database_parent": nested}[target]
    original = os.access
    monkeypatch.setattr(launcher.os, "access", lambda path, mode: False if path == denied else original(path, mode))
    monkeypatch.setattr(launcher.os, "execv", lambda *_: pytest.fail("must not serve"))
    assert launcher.main([]) == 1


@pytest.mark.parametrize("path", ["app/private/generated.py", "app/data/generated.py", "app/.env", "app/link.py"])
def test_build_refuses_private_paths_or_symlink_from_commit(tmp_path, path):
    repo = tmp_path / "generated-repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    for name in ("Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
                 "THIRD_PARTY_NOTICES.md", "app/config.py", "migrations/001_tables.sql",
                 "migrations/002_views.sql", "deploy/container_entrypoint.py"):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated input\n")
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if path == "app/link.py":
        target.symlink_to("config.py")
    else:
        target.write_text("synthetic excluded input\n")
    git("add", "--", "Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
        "THIRD_PARTY_NOTICES.md", "app", "migrations", "deploy")
    git("-c", "user.name=Generated Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "generated")
    output = tmp_path / "context.tar"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/build-image"), "--repository", str(repo),
                             "--revision", git("rev-parse", "HEAD"), "--export-context", str(output)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()
