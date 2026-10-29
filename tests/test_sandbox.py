"""Disposable sandbox acceptance. Only generated state and real loopback HTTP."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/sandbox"


def test_empty_real_factory_and_owned_cleanup():
    assert SCRIPT.is_file(), "public disposable sandbox launcher is missing"
    from tools.sandbox.runner import launch_sandbox

    with launch_sandbox("empty-v1") as run:
        root = run.fixture.root
        assert root.is_dir()
        assert run.origin.startswith("http://127.0.0.1:")
        assert run.fixture.expected.transaction_count == 0
        with httpx.Client(base_url=run.origin, trust_env=False) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/manage").status_code == 200
            assert client.get("/api/accounts").status_code == 503
        assert run.manifest_path.stat().st_mode & 0o077 == 0
    assert not root.exists()
    assert run.cleanup["process_reaped"] is True
    assert run.cleanup["root_removed"] is True


def readback(run):
    db = run.fixture.root / "state/ledger.sqlite"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return {
            "transactions": [dict(row) for row in conn.execute("SELECT * FROM transactions ORDER BY id")],
            "splits": [dict(row) for row in conn.execute("SELECT * FROM transaction_splits ORDER BY id")],
            "lines": [dict(row) for row in conn.execute("SELECT * FROM statement_lines ORDER BY id")],
            "relationships": [dict(row) for row in conn.execute("SELECT * FROM transaction_relationships")],
            "documents": [dict(row) for row in conn.execute("SELECT * FROM source_documents ORDER BY id")],
        }


def test_showcase_parallel_real_match_and_restart():
    from tools.sandbox.runner import launch_sandbox
    import hashlib

    with launch_sandbox("showcase-v1") as first, launch_sandbox("showcase-v1") as second:
        assert first.fixture.ids.transactions, "household scenario was not built"
        assert first.fixture.expected.income_cents == 425000
        assert first.fixture.expected.money_out_cents == 169787
        assert first.fixture.expected.ledger_net_cents == 255213
        assert first.fixture.expected.transfer_net_cents == 0
        assert first.fixture.root != second.fixture.root
        assert first.origin != second.origin
        manifest1 = json.loads(first.manifest_path.read_text())
        manifest2 = json.loads(second.manifest_path.read_text())
        assert manifest1["proof"]["database_identity"] != manifest2["proof"]["database_identity"]
        assert manifest1["network"]["no_packets"]
        assert len(manifest1["network"]["attempts_denied"]) >= 19
        assert "raw:connect_ex" in manifest1["network"]["attempts_denied"]
        assert "AF_INET6:connect" in manifest1["network"]["attempts_denied"]
        assert manifest1["provider_refusal"] == "egress_blocked"
        before = readback(first)
        assert len(before["transactions"]) == len(before["splits"]) == 7
        assert sum(row["amount_cents"] for row in before["splits"]) == 255213
        assert before["lines"][0]["match_status"] == "needs_review"
        assert before["lines"][0]["matched_transaction_id"] is None
        edge = before["relationships"][0]
        assert edge["relationship_kind"] == "transfer_pair" and edge["status"] == "active"
        assert edge["source_transaction_id"] == first.fixture.ids.transactions["transfer_out"]
        assert edge["target_transaction_id"] == first.fixture.ids.transactions["transfer_in"]
        original_hashes = {}
        for doc in before["documents"]:
            blob = first.fixture.root / "state/data" / doc["storage_ref"]
            digest = hashlib.sha256(blob.read_bytes()).hexdigest()
            assert digest == doc["sha256"]
            original_hashes[blob] = digest
        untouched = readback(second)
        with httpx.Client(base_url=first.origin, trust_env=False) as client:
            assert client.get("/recon?month=2026-06").status_code == 200
            response = client.post(f"/recon/line/{first.fixture.ids.statement_lines['market']}/confirm",
                                   data={"transaction_id": first.fixture.ids.transactions["market"],
                                         "month": first.fixture.month})
            assert response.status_code == 303, response.text
            assert client.get(response.headers["location"]).status_code == 200
        after = readback(first)
        assert len(after["transactions"]) == len(after["splits"]) == 7
        assert sum(row["amount_cents"] for row in after["splits"]) == 255213
        assert after["lines"][0]["match_status"] == "matched"
        assert after["lines"][0]["matched_transaction_id"] == first.fixture.ids.transactions["market"]
        market = next(row for row in after["transactions"] if row["id"] == first.fixture.ids.transactions["market"])
        assert market["amount_cents"] == -6842 and market["recon_status"] == "cleared"
        assert market["source_document_id"] == first.fixture.ids.documents["receipt"]
        assert readback(second) == untouched
        assert all(hashlib.sha256(blob.read_bytes()).hexdigest() == digest
                   for blob, digest in original_hashes.items())
        roots = (first.fixture.root, second.fixture.root)
    assert all(not root.exists() for root in roots)
    with launch_sandbox("showcase-v1") as fresh:
        assert fresh.fixture.root not in roots
        assert readback(fresh)["lines"][0]["match_status"] == "needs_review"


def test_replay_cli_executes_http_and_asserts_cleanup():
    result = subprocess.run([str(SCRIPT), "replay", "--scenario", "showcase-v1",
                             "--journey", "match-receipt-v1"], cwd=REPO,
                            capture_output=True, text=True, timeout=80)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "passed"
    assert receipt["http"]["method"] == "POST"
    assert receipt["http"]["status_code"] == 303
    assert receipt["persisted"]["matched"] is True
    assert receipt["persisted"]["transaction_count"] == receipt["persisted"]["split_count"] == 7
    assert receipt["cleanup"]["root_removed"] and receipt["cleanup"]["process_reaped"]
    assert not Path(receipt["root"]).exists()


def start_cli(args, *, cwd=REPO, env=None):
    proc = subprocess.Popen([str(SCRIPT), *args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    return proc


def ready(proc):
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=60), "setup did not emit readiness"
        line = proc.stdout.readline()
    assert line, proc.stderr.read()
    return json.loads(line)


def assert_dead(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_setup_actual_command_hostile_environment_signal_cleanup(tmp_path, sig):
    sentinel = tmp_path / "caller.sqlite"
    sentinel.write_bytes(b"caller-owned-do-not-touch")
    poison = tmp_path / ".env"
    poison.write_text(f"DB_PATH={sentinel}\nPLAID_ENABLED=true\nAPI_TOKEN=synthetic-poison\n")
    plugin = tmp_path / "sitecustomize.py"
    plugin.write_text("raise RuntimeError('untrusted Python startup executed')\n")
    env = {**os.environ, "DB_PATH": str(sentinel), "DATA_DIR": str(tmp_path),
           "HOME": str(tmp_path), "PYTHONPATH": str(tmp_path),
           "HTTPS_PROXY": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
           "API_TOKEN": "synthetic-poison", "PLAID_ENABLED": "true",
           "PLAID_CREDENTIALS_FILE": str(tmp_path / "not-a-credential"),
           "TELEGRAM_BOT_TOKEN": "synthetic-poison", "HERMES_CHAT_URL": "http://127.0.0.1:1",
           "INBOX_DIR": str(tmp_path), "LOCAL_LLM_BASE": "http://127.0.0.1:1",
           "EMBEDDINGS_ENABLED": "true", "MERCHANT_SEARCH_ENABLED": "true"}
    proc = start_cli(["setup", "--scenario", "showcase-v1"], cwd=tmp_path, env=env)
    root = None
    try:
        payload = ready(proc)
        root = Path(payload["fixture"]["root"])
        manifest = json.loads(Path(payload["manifest_path"]).read_text())
        child = manifest["child_pid"]
        assert manifest["lifespan"] == "off" and manifest["dotenv"] is False
        assert manifest["dotenv_refused"] is True
        assert manifest["settings"]["plaid_enabled"] is False
        assert manifest["settings"]["embeddings_enabled"] is False
        assert manifest["settings"]["merchant_search_enabled"] is False
        assert manifest["settings"]["strict_local_mode"] is True
        assert manifest["settings"]["plaid_credentials_file"] is None
        assert manifest["settings"]["hermes_chat_token_file"] is None
        assert manifest["settings"]["inbox_dir"] == ""
        assert manifest["settings"]["hermes_chat_url"] == ""
        assert manifest["cwd"] == str(root)
        assert manifest["home"] == str(root / "home")
        assert not {"PYTHONPATH", "HTTPS_PROXY", "HTTP_PROXY", "PLAID_CREDENTIALS_FILE"} & set(manifest["environment_keys"])
        with httpx.Client(base_url=payload["origin"], trust_env=False) as client:
            assert client.get("/activity?month=2026-06").status_code == 200
            assert client.get("/api/accounts").status_code == 503
            overview = client.get("/connections/data/overview")
            assert overview.status_code == 200
            assert overview.json()["setup"]["state"] == "disabled"
            refusal = client.post("/connections/data/link/start",
                                  json={"member_id": payload["fixture"]["ids"]["members"]["alex"]},
                                  headers={"Origin": payload["origin"],
                                           "X-Banking-CSRF": overview.json()["csrf"]})
            assert refusal.status_code == 403
            assert refusal.json()["detail"] == "trusted_origin_required"
        proc.send_signal(sig)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 130, (stdout, stderr)
        assert not root.exists()
        assert_dead(child)
        assert sentinel.read_bytes() == b"caller-owned-do-not-touch"
        assert poison.exists() and plugin.exists()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=15)
    assert root is not None and not root.exists()


@pytest.mark.parametrize("args", [
    ["setup", "--scenario", "nope"],
    ["replay", "--scenario", "showcase-v1", "--journey", "nope"],
    *[["setup", "--scenario", "empty-v1", flag, "synthetic-refused"] for flag in
      ("--db", "--state", "--root", "--source", "--service-url", "--reset-path",
       "--credentials", "--api-token", "--scen", "--port")],
])
def test_cli_rejects_unknown_or_authority_bearing_input(tmp_path, args):
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    result = subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert not result.stdout
    assert not tuple(tmp_path.iterdir()), "input refusal created state"


def test_deadline_reaps_real_child_and_owned_root(tmp_path, monkeypatch):
    import tools.sandbox.runner as runner

    monkeypatch.setattr(runner, "_READINESS_TIMEOUT", 0)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # Reset only the stdlib allocator cache, never Settings or socket functions.
    monkeypatch.setattr(runner.tempfile, "tempdir", None)
    created = []
    original = runner.subprocess.Popen

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", record)
    with pytest.raises(TimeoutError, match="readiness deadline"):
        with runner.launch_sandbox("empty-v1"):
            pytest.fail("deadline unexpectedly yielded")
    assert len(created) == 1 and created[0].poll() is not None
    assert_dead(created[0].pid)
    assert not tuple(tmp_path.iterdir())


def test_context_failure_survives_cleanup():
    from tools.sandbox.runner import launch_sandbox

    with pytest.raises(AssertionError, match="retained workload failure"):
        with launch_sandbox("empty-v1") as run:
            root = run.fixture.root
            raise AssertionError("retained workload failure")
    assert not root.exists()
    assert run.cleanup["process_reaped"]


def test_setup_does_not_advertise_dead_child_forever():
    proc = start_cli(["setup", "--scenario", "empty-v1"])
    try:
        payload = ready(proc)
        root = Path(payload["fixture"]["root"])
        child = json.loads(Path(payload["manifest_path"]).read_text())["child_pid"]
        os.kill(child, signal.SIGKILL)
        _, stderr = proc.communicate(timeout=8)
        assert proc.returncode != 0
        assert "child exited" in stderr
        assert not root.exists()
        assert_dead(child)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=15)
