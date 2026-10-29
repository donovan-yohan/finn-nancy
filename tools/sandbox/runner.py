"""One owned root, process group and prebound listener per trusted run."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
_READINESS_TIMEOUT = 40
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tools.sandbox.scenarios import SCENARIOS, ScenarioFixture, ScenarioName


@dataclass(frozen=True)
class SandboxRun:
    run_id: str
    origin: str
    fixture: ScenarioFixture
    manifest_path: Path
    cleanup: dict = field(default_factory=dict, compare=False)
    _process: subprocess.Popen | None = field(default=None, repr=False, compare=False)

    def payload(self) -> dict:
        return {"run_id": self.run_id, "origin": self.origin,
                "scenario": self.fixture.name, "month": self.fixture.month,
                "manifest_path": str(self.manifest_path), "fixture": self.fixture.payload()}


class SandboxInterrupted(Exception):
    pass


def _interrupt(signum, frame):
    raise SandboxInterrupted(f"signal {signum}")


def _environment(root: Path) -> dict[str, str]:
    # Deliberately do not inherit PATH, HOME, proxies, tokens, Python or Settings.
    return {
        "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TZ": "UTC",
        "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
        "XDG_CACHE_HOME": str(root / "cache"), "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "state"), "PYTHONDONTWRITEBYTECODE": "1",
        "DB_PATH": str(root / "state/ledger.sqlite"), "DATA_DIR": str(root / "state/data"),
        "CHAT_CHECKPOINT_DB": str(root / "state/checkpoints.sqlite"),
        "ADDR": "127.0.0.1:0", "READ_ONLY": "false", "STRICT_LOCAL_MODE": "true",
        "PLAID_ENABLED": "false", "EMBEDDINGS_ENABLED": "false",
        "MERCHANT_SEARCH_ENABLED": "false", "TRIAGE_ENABLED": "false",
        "VERIFIER_LLM_JUDGE": "false", "API_TOKEN": "", "LOCAL_LLM_KEY": "",
        "LOCAL_LLM_BASE": "http://127.0.0.1:1/v1", "LLM_TIMEOUT_S": "1",
        "LLM_MAX_RETRIES": "0", "HERMES_CHAT_URL": "", "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "", "INBOX_DIR": "", "BRAVE_API_KEY": "",
        "HOME_CURRENCY": "CAD",
    }


@contextmanager
def launch_sandbox(name: ScenarioName) -> Iterator[SandboxRun]:
    """Start a fresh real app. No caller state, URL, source or cleanup paths."""
    if name not in SCENARIOS:
        raise ValueError("unknown scenario")
    # TMPDIR is the approved scratch allocator, never an existing cleanup target.
    root = Path(tempfile.mkdtemp(prefix="finn-sandbox-"))
    os.chmod(root, 0o700)
    identity = root.stat()
    run_id = uuid.uuid4().hex
    owner = root / "owner.json"
    owner.write_text(json.dumps({"run_id": run_id, "supervisor_pid": os.getpid()}))
    for part in ("home", "tmp", "cache", "config", "state/data", "artifacts"):
        (root / part).mkdir(parents=True, exist_ok=True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proc = None
    run = None
    log = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        log = (root / "child.log").open("w+")
        command = [str(REPO / ".venv/bin/python"), "-I", "-B", "-c",
                   "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                   "from tools.sandbox.runner import _child; _child()", str(REPO)]
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                cwd=root, env=_environment(root), start_new_session=True,
                                pass_fds=(listener.fileno(),))
        proc.stdin.write(json.dumps({"root": str(root), "scenario": name,
                                     "fd": listener.fileno(), "origin": origin,
                                     "run_id": run_id}).encode())
        proc.stdin.close()
        listener.close()
        manifest_path = root / "manifest.json"
        import httpx

        deadline = time.monotonic() + _READINESS_TIMEOUT
        with httpx.Client(base_url=origin, trust_env=False, timeout=1) as client:
            while True:
                if proc.poll() is not None:
                    log.seek(0)
                    raise RuntimeError("sandbox child failed:\n" + log.read())
                if time.monotonic() >= deadline:
                    log.seek(0)
                    raise TimeoutError("sandbox readiness deadline:\n" + log.read())
                try:
                    if manifest_path.exists():
                        manifest = json.loads(manifest_path.read_text())
                        if client.get("/healthz").status_code == 200 and client.get("/manage").status_code == 200:
                            break
                except (httpx.HTTPError, json.JSONDecodeError):
                    pass
                time.sleep(0.05)
        assert manifest["run_id"] == run_id and manifest["origin"] == origin
        run = SandboxRun(run_id, origin, ScenarioFixture.from_payload(manifest["fixture"]),
                         manifest_path, _process=proc)
        yield run
        if proc.poll() is not None:
            raise RuntimeError("sandbox child exited during run")
    finally:
        listener.close()
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            # No child processes are admitted by the child audit policy.
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("owned process group remains alive")
        if log is not None:
            log.close()
        current = root.lstat()
        if (root.is_symlink() or (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino)
                or owner.is_symlink() or json.loads(owner.read_text())["run_id"] != run_id):
            raise RuntimeError("workspace ownership changed; refusing deletion")
        shutil.rmtree(root)
        if root.exists():
            raise RuntimeError("owned workspace cleanup failed")
        if run is not None:
            run.cleanup.update(process_reaped=True, root_removed=True, child_pid=proc.pid)


def _deny_network(internal_sender: socket.socket) -> dict:
    """Deny Python egress. Only accepted HTTP sockets and loop's own waker send."""
    import select
    import weakref

    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(4)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    accepted = weakref.WeakSet()
    original_accept = socket.socket.accept
    original_send = socket.socket.send
    original_sendall = socket.socket.sendall

    def deny(*args, **kwargs):
        raise PermissionError("sandbox denies outbound networking")

    def accept(sock, *args, **kwargs):
        peer, address = original_accept(sock, *args, **kwargs)
        accepted.add(peer)
        return peer, address

    def send(sock, *args, **kwargs):
        if sock is internal_sender or sock in accepted:
            return original_send(sock, *args, **kwargs)
        return deny()

    def sendall(sock, *args, **kwargs):
        if sock is internal_sender or sock in accepted:
            return original_sendall(sock, *args, **kwargs)
        return deny()

    socket.socket.accept = accept
    socket.socket.send = send
    socket.socket.sendall = sendall
    for method in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, method):
            setattr(socket.socket, method, deny)
    for method in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "socketpair"):
        setattr(socket, method, deny)

    def audit(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
                     "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
                     "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
            deny()

    sys.addaudithook(audit)
    checked = []
    for family, kind, address in ((socket.AF_INET, socket.SOCK_STREAM, tcp.getsockname()),
                                   (socket.AF_INET, socket.SOCK_DGRAM, udp.getsockname()),
                                   (socket.AF_UNIX, socket.SOCK_STREAM, "/unowned-sandbox-probe"),
                                   (socket.AF_INET6, socket.SOCK_STREAM, ("::1", 9)),
                                   (socket.AF_INET, socket.SOCK_STREAM, ("192.0.2.1", 443))):
        with socket.socket(family, kind) as probe:
            attempts = {"connect": lambda: probe.connect(address),
                        "connect_ex": lambda: probe.connect_ex(address),
                        "sendto": lambda: probe.sendto(b"denied", address),
                        "send": lambda: probe.send(b"denied"),
                        "sendall": lambda: probe.sendall(b"denied")}
            if hasattr(probe, "sendmsg"):
                attempts["sendmsg"] = lambda: probe.sendmsg([b"denied"], [], 0, address)
            for method, attempt in attempts.items():
                try:
                    attempt()
                except PermissionError:
                    checked.append(f"{family.name}:{method}")
                else:
                    raise RuntimeError("network self-check admitted outbound traffic")
    # The CPython audit hook also covers callers retaining raw _socket methods.
    import _socket

    raw = _socket.socket()
    try:
        for method in ("connect", "connect_ex"):
            try:
                getattr(raw, method)(tcp.getsockname())
            except PermissionError:
                checked.append("raw:" + method)
            else:
                raise RuntimeError("raw socket audit guard failed")
    finally:
        raw.close()
    for method in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        try:
            getattr(socket, method)("sandbox.example.invalid", 443)
        except PermissionError:
            checked.append(method)
        else:
            raise RuntimeError("DNS guard failed")
    assert not select.select([tcp, udp], [], [], 0.03)[0], "network probe sent packets"
    tcp.close()
    udp.close()
    return {"attempts_denied": checked, "no_packets": True}


def _child():
    """Private stdin protocol from our supervisor, never a CLI path capability."""
    import asyncio

    spec = json.loads(sys.stdin.read())
    root = Path(spec["root"])
    os.umask(0o077)
    # -I disables Python startup/environment injection; child already has allowlist.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    network = _deny_network(loop._csock)

    def refuse_dotenv(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            name = os.fsdecode(args[0]).rsplit("/", 1)[-1]
            if name == ".env" or name.startswith(".env."):
                raise PermissionError("sandbox refuses dotenv files")

    # Generated refusal probe, never inspect an operator dotenv.
    probe = root / ".env"
    probe.write_text("PLAID_ENABLED=true\\nAPI_TOKEN=synthetic-poison\\n")
    sys.addaudithook(refuse_dotenv)
    try:
        probe.read_bytes()
    except PermissionError:
        dotenv_refused = True
    else:
        raise RuntimeError("dotenv read was admitted")
    from app.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.api_token == "" and not settings.plaid_enabled
    probe.unlink()
    assert settings.db_path == root / "state/ledger.sqlite"
    assert settings.strict_local_mode and not settings.plaid_enabled
    from tools.sandbox.scenarios import build_scenario, verify_scenario

    fixture = build_scenario(root, spec["scenario"])
    proof = verify_scenario(fixture)
    from app.web.app import create_app
    import uvicorn

    app = create_app()
    from app.banking.client import BankingError

    try:
        app.state.banking_service.start_link(member_id=next(iter(fixture.ids.members.values()), 1), browser="synthetic-harness")
    except BankingError as exc:
        provider_refusal = str(exc)
        assert provider_refusal == "egress_blocked"
    else:
        raise RuntimeError("provider operation was admitted")
    listener = socket.socket(fileno=spec["fd"])
    assert listener.getsockname()[0] == "127.0.0.1"
    assert spec["origin"] == f"http://127.0.0.1:{listener.getsockname()[1]}"
    settings_keys = ("strict_local_mode", "plaid_enabled", "embeddings_enabled",
                     "merchant_search_enabled", "triage_enabled", "verifier_llm_judge",
                     "plaid_credentials_file", "plaid_private_dir", "hermes_chat_token_file",
                     "hermes_chat_url", "inbox_dir", "read_only")
    safe_settings = {key: getattr(settings, key) for key in settings_keys}
    assert all(not safe_settings[key] for key in settings_keys if key != "strict_local_mode")
    payload = {"run_id": spec["run_id"], "origin": spec["origin"],
               "child_pid": os.getpid(), "fixture": fixture.payload(), "proof": proof,
               "settings": safe_settings, "provider_refusal": provider_refusal,
               "network": network, "lifespan": "off", "dotenv": False, "dotenv_refused": dotenv_refused,
               "environment_keys": sorted(os.environ), "cwd": str(Path.cwd()),
               "home": os.environ["HOME"]}
    (root / "manifest.json").write_text(json.dumps(payload, indent=2))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", loop="asyncio",
                                          log_level="warning", access_log=False))
    try:
        loop.run_until_complete(server.serve(sockets=[listener]))
    finally:
        listener.close()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def replay(name: ScenarioName, journey: str) -> dict:
    """Drive only the fixed match action over HTTP, then read durable state."""
    if name not in ("showcase-v1", "household-v1") or journey != "match-receipt-v1":
        raise ValueError("journey requires showcase-v1 or household-v1 and match-receipt-v1")
    import csv
    import hashlib
    import io
    import sqlite3
    import httpx

    with launch_sandbox(name) as run:
        fixture = run.fixture
        ids = fixture.ids
        expected = fixture.expected
        initial = json.loads(run.manifest_path.read_text())["proof"]
        initial["originals"] = [{"document_id": d["id"], "sha256": d["sha256"]}
                                for d in initial["originals"]]
        with httpx.Client(base_url=run.origin, trust_env=False, timeout=10) as client:
            exported = client.get("/statements/export", params={"month": fixture.month, "format": "csv"})
            assert exported.status_code == 200
            initial_rows = list(csv.DictReader(io.StringIO(exported.text)))
            initial["report_totals"] = {row["key"]: int(row["cents"]) for row in initial_rows
                                        if row["record_type"] == "household_total"}
            assert {key: initial["report_totals"][key] for key in
                    ("income", "net_money_out", "transfer_neutrality_control")} == {
                "income": expected.income_cents, "net_money_out": expected.money_out_cents,
                "transfer_neutrality_control": expected.transfer_net_cents}
            assert client.get(f"/recon?month={fixture.month}").status_code == 200
            path = f"/recon/line/{ids.statement_lines['market']}/confirm"
            response = client.post(path, data={"month": fixture.month,
                                              "transaction_id": ids.transactions["market"]})
            assert response.status_code == 303, response.text
            assert client.get(response.headers["location"]).status_code == 200
            exported = client.get("/statements/export", params={"month": fixture.month, "format": "csv"})
            assert exported.status_code == 200
            rows = list(csv.DictReader(io.StringIO(exported.text)))
            totals = {row["key"]: int(row["cents"]) for row in rows if row["record_type"] == "household_total"}
            assert {key: totals[key] for key in ("income", "net_money_out", "transfer_neutrality_control")} == {
                "income": expected.income_cents, "net_money_out": expected.money_out_cents,
                "transfer_neutrality_control": expected.transfer_net_cents}
            assert totals == initial["report_totals"]
        db = fixture.root / "state/ledger.sqlite"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            counts = tuple(conn.execute("SELECT (SELECT count(*) FROM transactions),"
                                        "(SELECT count(*) FROM transaction_splits),"
                                        "(SELECT sum(amount_cents) FROM transaction_splits)").fetchone())
            assert counts == (expected.transaction_count, expected.split_count, expected.ledger_net_cents)
            assert not conn.execute("SELECT t.id FROM transactions t LEFT JOIN transaction_splits s "
                                    "ON s.transaction_id=t.id GROUP BY t.id "
                                    "HAVING t.amount_cents != coalesce(sum(s.amount_cents),0)").fetchall()
            line = dict(conn.execute("SELECT * FROM statement_lines WHERE id=?", (ids.statement_lines["market"],)).fetchone())
            assert line["match_status"] == "matched" and line["matched_transaction_id"] == ids.transactions["market"]
            market = conn.execute("SELECT * FROM transactions WHERE id=?", (ids.transactions["market"],)).fetchone()
            assert market["amount_cents"] == -6842 and market["recon_status"] == "cleared"
            assert market["source_document_id"] == ids.documents["receipt"]
            originals = []
            for doc in conn.execute("SELECT * FROM source_documents ORDER BY id"):
                digest = hashlib.sha256((fixture.root / "state/data" / doc["storage_ref"]).read_bytes()).hexdigest()
                assert digest == doc["sha256"]
                originals.append({"document_id": doc["id"], "sha256": digest})
            category_resolution = dict(conn.execute(
                "SELECT * FROM v_expense_resolution_monthly_control WHERE month=?", (fixture.month,)).fetchone())
            budgets = {r["name"]: {"cents": r["amount_cents"], "owner": r["owner_name"]}
                       for r in conn.execute("SELECT c.name,b.amount_cents,coalesce(h.name,'Shared') AS owner_name "
                           "FROM budgets b JOIN categories c ON c.id=b.category_id "
                           "LEFT JOIN household_members h ON h.id=b.owner_member_id WHERE b.period_month=''")}
            members = {key: conn.execute("SELECT name FROM household_members WHERE id=?", (value,)).fetchone()[0]
                       for key, value in ids.members.items()}
        accounts = {r["account_name"]: int(r["cents"]) for r in rows
                    if r["record_type"] == "account_total" and r["key"] == "ledger_closing_balance"}
        expenses = [r for r in rows if r["record_type"] == "evidence_row" and r["flow_kind"] == "purchase"]
        assert all(r["resolution_disposition"] == "human_approved" and r["merchant_claim_ids"]
                   and r["category_claim_ids"] for r in expenses)
        assert accounts == initial["accounts"] and members == initial["members"]
        assert budgets == initial["budgets"] and category_resolution == initial["category_resolution"]
        close_state = next(r["close_state"] for r in rows if r["record_type"] == "metadata")
        assert close_state == initial["close_state"]
        assert originals == initial["originals"]
        result = {"status": "passed", "journey": journey, "run_id": run.run_id,
                  "fixture": fixture.payload(), "initial": initial,
                  "root": str(fixture.root), "origin": run.origin,
                  "http": {"method": "POST", "path": path, "status_code": response.status_code},
                  "persisted": {"matched": True, "transaction_count": counts[0], "split_count": counts[1],
                                "accounts": accounts, "members": members, "budgets": budgets,
                                "category_resolution": category_resolution, "close_state": close_state,
                                "ledger_net_cents": counts[2], "report_totals": totals, "originals": originals}}
    assert run.cleanup["process_reaped"] and run.cleanup["root_removed"]
    return {**result, "cleanup": run.cleanup}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", allow_abbrev=False)
    setup.add_argument("--scenario", choices=SCENARIOS, required=True)
    replay_parser = sub.add_parser("replay", allow_abbrev=False)
    replay_parser.add_argument("--scenario", choices=("showcase-v1", "household-v1"), required=True)
    replay_parser.add_argument("--journey", choices=("match-receipt-v1",), required=True)
    args = parser.parse_args()
    old = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if args.command == "replay":
            print(json.dumps(replay(args.scenario, args.journey)), flush=True)
            return 0
        with launch_sandbox(args.scenario) as run:
            print(json.dumps(run.payload()), flush=True)
            assert run._process is not None
            while True:
                if run._process.poll() is not None:
                    raise RuntimeError("sandbox child exited during setup")
                time.sleep(0.2)
    except SandboxInterrupted:
        return 130
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
