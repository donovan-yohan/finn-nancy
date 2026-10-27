"""Exercise the real Node launcher with generated children, never a user browser."""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/js/test_ui_theme_browser.cjs"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_browser_command_keeps_minimal_environment_and_platform_session(tmp_path, monkeypatch, platform):
    from tests import test_ui_theme_browser as theme_browser

    node, session = "/generated/bin/node", "/generated/bin/dbus-run-session"
    chrome = "/generated/bin/chrome"
    monkeypatch.setattr(theme_browser, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(theme_browser, "shutil", SimpleNamespace(
        which=lambda name: {"node": node, "dbus-run-session": session,
                            "dbus-daemon": "/generated/bin/dbus-daemon"}.get(name),
    ))
    for key in ("DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID", "NODE_OPTIONS",
                "GENERATED_PRIVATE_SENTINEL"):
        monkeypatch.setenv(key, "must-not-inherit")
    monkeypatch.setenv("CHROMIUM_BIN", chrome)
    monkeypatch.setenv("PATH", "/generated/bin")
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "generated exact output\n", "generated stderr\n")

    def popen(argv, **kwargs):
        observed.append((argv, kwargs))

        def communicate(*, timeout):
            assert timeout == 60
            return "generated exact output\n", "generated stderr\n"

        return SimpleNamespace(communicate=communicate, returncode=0,
                               stdout=io.StringIO(), stderr=io.StringIO())

    monkeypatch.setattr(theme_browser, "subprocess", SimpleNamespace(
        run=run, Popen=popen, PIPE=subprocess.PIPE, CompletedProcess=subprocess.CompletedProcess,
    ))
    site = tmp_path / "site with spaces"
    assert theme_browser.run_browser(site, tmp_path, "first-install-offline") == "generated exact output\n"
    command = [node, str(HARNESS), str(site), str(tmp_path / "chrome"), "first-install-offline"]
    if platform == "linux":
        command = [session, "--", *command]
    environment = {"PATH": "/generated/bin", "TMPDIR": str(tmp_path), "CHROMIUM_BIN": chrome}
    homes = [("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
             ("XDG_DATA_HOME", "data")]
    if platform != "darwin":
        homes.append(("HOME", "home"))
    for variable, directory in homes:
        environment[variable] = str(tmp_path / "browser-environment" / directory)
    options = {"cwd": tmp_path, "env": environment, "text": True}
    if platform == "linux":
        options.update(start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        options.update(capture_output=True, timeout=60, check=False)
    assert observed == [(command, options)]


@pytest.mark.parametrize("missing", ["dbus-run-session", "dbus-daemon"])
def test_linux_browser_missing_session_tool_fails_before_launch(tmp_path, monkeypatch, missing):
    from tests import test_ui_theme_browser as theme_browser

    monkeypatch.setattr(theme_browser, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("CHROMIUM_BIN", "/generated/chrome")
    monkeypatch.setattr(theme_browser, "shutil", SimpleNamespace(
        which=lambda name: None if name == missing else "/generated/" + name,
    ))

    def must_not_launch(*args, **kwargs):
        pytest.fail("missing session prerequisite must fail before launching Node")

    monkeypatch.setattr(theme_browser, "subprocess", SimpleNamespace(run=must_not_launch))
    with pytest.raises(AssertionError, match=missing + " is required"):
        theme_browser.run_browser(tmp_path / "site", tmp_path, "focus-light")


@pytest.mark.parametrize("cleanup", ["ordinary", "group-gone", "kill-fails", "reap-timeout", "interrupted"])
def test_linux_session_cleanup_preserves_original_failure(tmp_path, monkeypatch, cleanup):
    from tests import test_ui_theme_browser as theme_browser

    monkeypatch.setattr(theme_browser, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(theme_browser, "shutil", SimpleNamespace(which=lambda name: "/generated/" + name))
    monkeypatch.setenv("CHROMIUM_BIN", "/generated/chrome")
    original = (KeyboardInterrupt("generated interrupt") if cleanup == "interrupted" else
                subprocess.TimeoutExpired(["generated original argv"], 60, b"partial out", b"partial err"))
    calls = []

    def communicate(*, timeout):
        assert timeout == 60
        raise original

    def killpg(pid, sig):
        calls.append(("killpg", pid, sig))
        if cleanup == "group-gone":
            raise ProcessLookupError("generated absent group")
        if cleanup == "kill-fails":
            raise PermissionError("generated cleanup failure")

    def wait(*, timeout):
        calls.append(("wait", timeout))
        if cleanup == "reap-timeout":
            raise subprocess.TimeoutExpired("generated wait", timeout)

    process = SimpleNamespace(pid=123456, communicate=communicate, wait=wait,
                              stdout=io.StringIO(), stderr=io.StringIO())
    monkeypatch.setattr(theme_browser, "os", SimpleNamespace(environ=os.environ, killpg=killpg))
    monkeypatch.setattr(theme_browser, "subprocess", SimpleNamespace(
        Popen=lambda *args, **kwargs: process, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
    ))
    with pytest.raises(type(original)) as failure:
        theme_browser.run_browser(tmp_path / "site", tmp_path, "focus-light")
    assert failure.value is original
    assert calls == [("killpg", process.pid, signal.SIGKILL)] + ([] if cleanup == "kill-fails" else [("wait", 1)])
    assert process.stdout.closed and process.stderr.closed
    notes = getattr(original, "__notes__", [])
    assert bool(notes) == (cleanup in {"kill-fails", "reap-timeout"})
    if notes:
        assert "Linux browser-session cleanup failed" in notes[0]


def test_linux_session_launch_error_is_not_replaced_by_cleanup(tmp_path, monkeypatch):
    from tests import test_ui_theme_browser as theme_browser

    missing = str(tmp_path / "generated-missing-session")
    monkeypatch.setattr(theme_browser, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(theme_browser, "shutil", SimpleNamespace(
        which=lambda name: missing if name == "dbus-run-session" else "/generated/" + name,
    ))
    monkeypatch.setenv("CHROMIUM_BIN", "/generated/chrome")

    def must_not_kill(*args):
        pytest.fail("failed launch owns no process group")

    monkeypatch.setattr(theme_browser, "os", SimpleNamespace(environ=os.environ, killpg=must_not_kill))
    with pytest.raises(FileNotFoundError) as failure:
        theme_browser.run_browser(tmp_path / "site", tmp_path, "focus-light")
    assert failure.value.filename == missing


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_browser_environment_uses_writable_generated_home_not_callers(tmp_path, monkeypatch, platform):
    from tests import test_ui_theme_browser as theme_browser

    # Change only the helper's platform selector, not pytest's global sys module.
    monkeypatch.setattr(theme_browser, "sys", SimpleNamespace(platform=platform), raising=False)
    run_browser = theme_browser.run_browser

    node = shutil.which("node")
    assert node, "Node is required for browser launcher regressions"
    if platform == "linux" and os.uname().sysname != "Linux":
        # This cross-platform case proves generated HOME/XDG, not a real bus.
        # Linux runs the actual session tool; macOS uses a transparent argv shim.
        session = tmp_path / "generated-session-shim"
        session.write_text(f"#!{node}\n"
                           "const {spawnSync}=require('node:child_process');\n"
                           "const r=spawnSync(process.argv[3],process.argv.slice(4),{stdio:'inherit'});\n"
                           "process.exit(r.status ?? 1);\n")
        session.chmod(0o700)
        original_which = shutil.which
        monkeypatch.setattr(theme_browser, "shutil", SimpleNamespace(
            which=lambda name: (str(session) if name in {"dbus-run-session", "dbus-daemon"}
                                else original_which(name)),
        ))
    browser = tmp_path / "generated-browser"
    observed = tmp_path / "environment.json"
    marker = "generated environment observation complete"
    variables = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")
    for name in (*variables, "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.setenv(name, str(tmp_path / "must-not-inherit" / name))
    monkeypatch.setenv("GENERATED_PRIVATE_SENTINEL", "must-not-inherit")
    browser.write_text(
        f"#!{node}\n"
        "const fs = require('node:fs');\n"
        f"const names = {json.dumps([*variables, 'GENERATED_PRIVATE_SENTINEL', 'DBUS_SESSION_BUS_ADDRESS'])};\n"
        "const observed = Object.fromEntries(names.map(name => [name, process.env[name] ?? null]));\n"
        f"fs.writeFileSync({str(observed)!r}, JSON.stringify(observed));\n"
        f"fs.writeSync(2, {marker!r}); process.exit(23);\n"
    )
    browser.chmod(0o700)
    monkeypatch.setenv("CHROMIUM_BIN", str(browser))
    with pytest.raises(AssertionError, match=marker):
        run_browser(tmp_path / "site", tmp_path, "focus-light")
    environment = json.loads(observed.read_text())
    assert environment.pop("GENERATED_PRIVATE_SENTINEL") is None
    address = environment.pop("DBUS_SESSION_BUS_ADDRESS")
    if platform == "linux" and os.uname().sysname == "Linux":
        assert address and address.startswith("unix:")
        assert address != str(tmp_path / "must-not-inherit" / "DBUS_SESSION_BUS_ADDRESS")
    else:
        assert address is None
    if platform == "darwin":
        assert environment.pop("HOME") is None
        variables = variables[1:]
    paths = []
    for name in variables:
        value = environment[name]
        assert value, f"{name} must name a generated writable directory, not be absent"
        directory = Path(value)
        assert directory.is_relative_to(tmp_path)
        assert not directory.is_relative_to(tmp_path / "must-not-inherit")
        assert directory.is_dir()
        probe = directory / "generated-write-probe"
        probe.write_text("generated")
        probe.unlink()
        paths.append(directory)
    assert len(set(paths)) == len(variables)


@pytest.mark.skipif(sys.platform != "linux", reason="real private-session lifetime requires Linux")
@pytest.mark.parametrize("outcome", ["success", "exit", "signal"])
def test_linux_private_session_reaches_child_and_terminates_with_node(tmp_path, monkeypatch, outcome):
    from tests import test_ui_theme_browser as theme_browser

    # Real Node and dbus-run-session, but a generated Node program, not Chrome.
    node = shutil.which("node")
    assert node, "Node is required for browser launcher regressions"
    monkeypatch.setattr(theme_browser, "ROOT", tmp_path)
    monkeypatch.setenv("CHROMIUM_BIN", "/generated/unused-browser")
    monkeypatch.setenv("GENERATED_PRIVATE_SENTINEL", "must-not-inherit")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/generated-parent-must-not-inherit")
    program = tmp_path / "tests/js/test_ui_theme_browser.cjs"
    program.parent.mkdir(parents=True)
    observed = tmp_path / "child.json"
    marker = "generated session result\n"
    program.write_text(
        "const fs=require('node:fs');\n"
        "const children=fs.readFileSync(`/proc/${process.ppid}/task/${process.ppid}/children`,'utf8').trim().split(/\\s+/);\n"
        "const daemon=children.find(pid=>{try{return fs.readFileSync(`/proc/${pid}/comm`,'utf8').trim()==='dbus-daemon';}catch{return false;}});\n"
        f"fs.writeFileSync({str(observed)!r},JSON.stringify({{pid:process.pid,daemon:Number(daemon),"
        "address:process.env.DBUS_SESSION_BUS_ADDRESS??null,"
        "sentinel:process.env.GENERATED_PRIVATE_SENTINEL??null,args:process.argv.slice(2)}));\n"
        f"fs.writeSync(1,{marker!r});fs.writeSync(2,'generated session stderr');\n"
        + {"success": "process.exit(0);\n", "exit": "process.exit(23);\n",
           "signal": "process.kill(process.pid,'SIGTERM');\n"}[outcome]
    )
    site = tmp_path / "site with spaces"
    results = []
    def observe_result(*args, **kwargs):
        result = subprocess.CompletedProcess(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(theme_browser, "subprocess", SimpleNamespace(
        Popen=subprocess.Popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=observe_result,
    ))
    if outcome == "success":
        assert theme_browser.run_browser(site, tmp_path, "focus-light") == marker
    else:
        with pytest.raises(AssertionError, match="generated session result") as failure:
            theme_browser.run_browser(site, tmp_path, "focus-light")
        assert "generated session stderr" in str(failure.value)
    assert len(results) == 1
    assert results[0].returncode == {"success": 0, "exit": 23, "signal": 143}[outcome]
    assert results[0].stdout == marker
    assert "generated session stderr" in results[0].stderr
    observation = json.loads(observed.read_text())
    assert observation["address"] and observation["address"].startswith("unix:")
    assert observation["address"] != "unix:path=/generated-parent-must-not-inherit"
    assert observation["sentinel"] is None
    assert observation["args"] == [str(site), str(tmp_path / "chrome"), "focus-light"]
    assert observation["daemon"] > 0
    for pid in (observation["pid"], observation["daemon"]):
        status = Path(f"/proc/{pid}/status")
        deadline = time.monotonic() + 1
        while status.exists() and "\nState:\tZ" not in status.read_text() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not status.exists() or "\nState:\tZ" in status.read_text(), f"owned process {pid} survived"


@pytest.mark.skipif(sys.platform != "linux", reason="real private-session lifetime requires Linux")
def test_linux_private_session_outer_timeout_reaps_owned_processes(tmp_path, monkeypatch):
    from tests import test_ui_theme_browser as theme_browser

    # Exercise the real, unchanged 60s timeout; no Chrome or mocked subprocess.
    monkeypatch.setattr(theme_browser, "ROOT", tmp_path)
    monkeypatch.setenv("CHROMIUM_BIN", "/generated/unused-browser")
    program = tmp_path / "tests/js/test_ui_theme_browser.cjs"
    program.parent.mkdir(parents=True)
    observed = tmp_path / "owned.json"
    program.write_text(
        "const fs=require('node:fs'),{spawn}=require('node:child_process');\n"
        "const children=fs.readFileSync(`/proc/${process.ppid}/task/${process.ppid}/children`,'utf8').trim().split(/\\s+/);\n"
        "const daemon=children.find(pid=>{try{return fs.readFileSync(`/proc/${pid}/comm`,'utf8').trim()==='dbus-daemon';}catch{return false;}});\n"
        "const child=spawn(process.execPath,['-e',\"process.on('SIGTERM',()=>{});setInterval(()=>{},1000);\"],{stdio:'inherit'});\n"
        f"fs.writeFileSync({str(observed)!r},JSON.stringify({{node:process.pid,wrapper:process.ppid,daemon:Number(daemon),child:child.pid}}));\n"
        "fs.writeSync(1,'generated outer stdout\\n');fs.writeSync(2,'generated outer stderr\\n');\n"
        "process.on('SIGTERM',()=>{});setInterval(()=>{},1000);\n"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as failure:
            theme_browser.run_browser(tmp_path / "site", tmp_path, "focus-light")
        assert 60 <= time.monotonic() - started < 63
        assert failure.value.timeout == 60
        assert failure.value.cmd == [shutil.which("dbus-run-session"), "--", shutil.which("node"),
                                     str(program), str(tmp_path / "site"), str(tmp_path / "chrome"), "focus-light"]
        assert failure.value.output == b"generated outer stdout\n"
        assert b"generated outer stderr\n" in failure.value.stderr
        owned = json.loads(observed.read_text())
        assert all(pid > 0 for pid in owned.values())
        deadline = time.monotonic() + 1
        survivors = owned
        while survivors and time.monotonic() < deadline:
            survivors = {name: pid for name, pid in owned.items()
                         if (status := Path(f"/proc/{pid}/status")).exists()
                         and "\nState:\tZ" not in status.read_text()}
            if survivors:
                time.sleep(.01)
        assert not survivors, f"owned processes survived outer timeout: {survivors}"
    finally:
        # A failing predecessor must not leak this regression's generated tree.
        if observed.exists():
            for pid in json.loads(observed.read_text()).values():
                if pid > 0:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass


@pytest.mark.skipif(sys.platform != "linux", reason="real private-session lifetime requires Linux")
def test_linux_private_session_preserves_browser_deadline_and_cleanup(tmp_path, monkeypatch):
    from tests import test_ui_theme_browser as theme_browser

    node = shutil.which("node")
    assert node, "Node is required for browser launcher regressions"
    browser = tmp_path / "generated-browser"
    observed = tmp_path / "owned.json"
    browser.write_text(
        f"#!{node}\n"
        "const fs=require('node:fs');\n"
        "const parent=fs.readFileSync(`/proc/${process.ppid}/status`,'utf8').match(/^PPid:\\s+(\\d+)/m)[1];\n"
        "const children=fs.readFileSync(`/proc/${parent}/task/${parent}/children`,'utf8').trim().split(/\\s+/);\n"
        "const daemon=children.find(pid=>{try{return fs.readFileSync(`/proc/${pid}/comm`,'utf8').trim()==='dbus-daemon';}catch{return false;}});\n"
        f"fs.writeFileSync({str(observed)!r},JSON.stringify({{browser:process.pid,node:process.ppid,daemon:Number(daemon)}}));\n"
        "process.on('SIGTERM',()=>{});setInterval(()=>{},1000);\n"
    )
    browser.chmod(0o700)
    monkeypatch.setenv("CHROMIUM_BIN", str(browser))
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError, match="startup deadline exceeded"):
            theme_browser.run_browser(tmp_path / "site", tmp_path, "focus-light")
        assert 15 <= time.monotonic() - started < 19
        owned = json.loads(observed.read_text())
        assert owned["daemon"] > 0
        for pid in owned.values():
            status = Path(f"/proc/{pid}/status")
            deadline = time.monotonic() + 1
            while status.exists() and "\nState:\tZ" not in status.read_text() and time.monotonic() < deadline:
                time.sleep(.01)
            assert not status.exists() or "\nState:\tZ" in status.read_text(), f"owned process {pid} survived"
    finally:
        if observed.exists():
            for pid in json.loads(observed.read_text()).values():
                if pid > 0:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass


@pytest.mark.parametrize("response_mode", [
    "delayed-headers", "delayed-body", "delayed-headers-past-old-budget",
    "delayed-body-past-old-budget", "stalled-headers", "stalled-body", "polled-then-stalled-body",
])
def test_browser_readiness_allows_slow_response_within_startup_budget(tmp_path, response_mode):
    node = shutil.which("node")
    assert node, "Node is required for browser launcher regressions"
    browser = tmp_path / "generated-browser"
    pid_file = tmp_path / "child.pid"
    requests = tmp_path / "requests.log"
    preload = tmp_path / "observe-handoff.cjs"
    marker = "generated page target reached WebSocket handoff"
    # Phase-only sentinel, not a full browser journey: HTTP polling and child lifecycle are real.
    preload.write_text(
        "global.WebSocket = class { constructor() {\n"
        f"  throw Error({marker!r});\n"
        "} };\n"
    )
    browser.write_text(
        f"#!{node}\n"
        "const fs = require('node:fs'), http = require('node:http');\n"
        f"fs.writeFileSync({str(pid_file)!r}, String(process.pid));\n"
        "const port = Number(process.argv.find(arg => arg.startsWith('--remote-debugging-port=')).split('=')[1]);\n"
        "const body = JSON.stringify([{type:'page',webSocketDebuggerUrl:'ws://127.0.0.1:' + port + '/generated'}]);\n"
        "let attempts = 0;\n"
        "http.createServer((req,res) => {\n"
        f"  fs.appendFileSync({str(requests)!r}, req.url + '\\n');\n"
        + {
            "delayed-headers": "  setTimeout(() => res.end(body), 150);\n",
            "delayed-body": "  res.writeHead(200, {'Content-Type':'application/json'}); res.flushHeaders(); setTimeout(() => res.end(body), 150);\n",
            "delayed-headers-past-old-budget": "  setTimeout(() => res.end(body), 6000);\n",
            "delayed-body-past-old-budget": "  res.writeHead(200, {'Content-Type':'application/json'}); res.flushHeaders(); setTimeout(() => res.end(body), 6000);\n",
            "stalled-headers": "  // Leave headers pending until launcher cleanup.\n",
            "stalled-body": "  res.writeHead(200, {'Content-Type':'application/json'}); res.flushHeaders(); res.write('[');\n",
            "polled-then-stalled-body": (
                "  if (++attempts <= 6) { setTimeout(() => res.end('[]'), 1000); return; }\n"
                "  res.writeHead(200, {'Content-Type':'application/json'}); res.flushHeaders(); res.write('[');\n"
            ),
        }[response_mode]
        + "}).listen(port, '127.0.0.1');\n"
    )
    browser.chmod(0o700)
    started = time.monotonic()
    try:
        result = subprocess.run(
            [node, "--require", str(preload), str(HARNESS), str(tmp_path / "site"),
             str(tmp_path / "chrome"), "focus-light"],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmp_path),
                 "CHROMIUM_BIN": str(browser)},
            text=True, capture_output=True, timeout=19, check=False,
        )
        elapsed = time.monotonic() - started
        assert requests.read_text().splitlines(), "readiness must reach the real generated HTTP listener"
        assert set(requests.read_text().splitlines()) == {"/json/list"}
        assert result.returncode == 1, result.stdout + result.stderr
        if response_mode.startswith("delayed"):
            assert marker in result.stderr, result.stderr
            assert "startup deadline" not in result.stderr
            if response_mode.endswith("past-old-budget"):
                assert 6 <= elapsed < 15
        else:
            assert "startup deadline" in result.stderr, result.stderr
            assert marker not in result.stderr
            assert 15 <= elapsed < 19
            if response_mode == "polled-then-stalled-body":
                assert len(requests.read_text().splitlines()) == 7, "polls must not reset the aggregate budget"
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("failure", ["exit", "signal", "missing", "unresponsive"])
def test_browser_startup_failure_reports_cause_and_reaps_owned_child(tmp_path, failure):
    node = shutil.which("node")
    assert node, "Node is required for browser launcher regressions"
    browser = tmp_path / "generated-browser"
    pid_file = tmp_path / "child.pid"
    marker = "generated browser startup diagnostic"
    browser.write_text(
        f"#!{node}\n"
        "const fs = require('node:fs');\n"
        f"fs.writeFileSync({str(pid_file)!r}, String(process.pid));\n"
        f"fs.writeSync(2, {marker!r} + '\\n');\n"
        + {
            "exit": "process.exit(23);\n",
            "signal": "process.kill(process.pid, 'SIGTERM');\n",
            "missing": "process.exit(0);\n",
            # The startup budget must stay bounded even if termination is ignored.
            "unresponsive": "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);\n",
        }[failure]
    )
    browser.chmod(0o700)
    if failure == "missing":
        browser.unlink()
    profile = tmp_path / "chrome"
    try:
        try:
            result = subprocess.run(
                [node, str(HARNESS), str(tmp_path / "site"), str(profile), "focus-light"],
                cwd=tmp_path,
                env={"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmp_path),
                     "CHROMIUM_BIN": str(browser)},
                text=True, capture_output=True,
                timeout=19 if failure == "unresponsive" else 3, check=False,
            )
        except subprocess.TimeoutExpired:
            raise AssertionError("browser launcher hung instead of reporting startup failure and cleaning up") from None
        assert result.returncode == 1, result.stdout + result.stderr
        assert str(browser) in result.stderr
        if failure == "missing":
            assert "ENOENT" in result.stderr
        else:
            assert marker in result.stderr
            assert marker in Path(str(profile) + ".stderr.log").read_text()
            if failure == "exit":
                assert "code=23" in result.stderr
            elif failure == "signal":
                assert "signal=SIGTERM" in result.stderr
            else:
                assert "startup deadline" in result.stderr
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
    finally:
        # A broken launcher must not leak this regression's generated child.
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass
