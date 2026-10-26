"""Exercise the real Node launcher with generated children, never a user browser."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/js/test_ui_theme_browser.cjs"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_browser_environment_uses_writable_generated_home_not_callers(tmp_path, monkeypatch, platform):
    from tests import test_ui_theme_browser as theme_browser

    # Change only the helper's platform selector, not pytest's global sys module.
    monkeypatch.setattr(theme_browser, "sys", SimpleNamespace(platform=platform), raising=False)
    run_browser = theme_browser.run_browser

    node = shutil.which("node")
    assert node, "Node is required for browser launcher regressions"
    browser = tmp_path / "generated-browser"
    observed = tmp_path / "environment.json"
    marker = "generated environment observation complete"
    variables = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")
    for name in variables:
        monkeypatch.setenv(name, str(tmp_path / "must-not-inherit" / name))
    monkeypatch.setenv("GENERATED_PRIVATE_SENTINEL", "must-not-inherit")
    browser.write_text(
        f"#!{node}\n"
        "const fs = require('node:fs');\n"
        f"const names = {json.dumps([*variables, 'GENERATED_PRIVATE_SENTINEL'])};\n"
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


@pytest.mark.parametrize("response_mode", ["delayed-headers", "delayed-body", "stalled-headers", "stalled-body"])
def test_browser_readiness_allows_slow_response_within_startup_budget(tmp_path, response_mode):
    node = shutil.which("node")
    assert node, "Node is required for browser launcher regressions"
    browser = tmp_path / "generated-browser"
    pid_file = tmp_path / "child.pid"
    requests = tmp_path / "requests.log"
    preload = tmp_path / "observe-handoff.cjs"
    marker = "generated page target reached WebSocket handoff"
    # Observe just the readiness handoff; HTTP polling and child lifecycle are real.
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
        "http.createServer((req,res) => {\n"
        f"  fs.appendFileSync({str(requests)!r}, req.url + '\\n');\n"
        + {
            "delayed-headers": "  setTimeout(() => res.end(body), 150);\n",
            "delayed-body": "  res.writeHead(200, {'Content-Type':'application/json'}); res.flushHeaders(); setTimeout(() => res.end(body), 150);\n",
            "stalled-headers": "  // Leave headers pending until launcher cleanup.\n",
            "stalled-body": "  res.writeHead(200, {'Content-Type':'application/json'}); res.flushHeaders(); res.write('[');\n",
        }[response_mode]
        + "}).listen(port, '127.0.0.1');\n"
    )
    browser.chmod(0o700)
    try:
        result = subprocess.run(
            [node, "--require", str(preload), str(HARNESS), str(tmp_path / "site"),
             str(tmp_path / "chrome"), "focus-light"],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmp_path),
                 "CHROMIUM_BIN": str(browser)},
            text=True, capture_output=True, timeout=9, check=False,
        )
        assert requests.read_text().splitlines(), "readiness must reach the real generated HTTP listener"
        assert set(requests.read_text().splitlines()) == {"/json/list"}
        assert result.returncode == 1, result.stdout + result.stderr
        if response_mode.startswith("delayed"):
            assert marker in result.stderr, result.stderr
            assert "startup deadline" not in result.stderr
        else:
            assert "startup deadline" in result.stderr, result.stderr
            assert marker not in result.stderr
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
                timeout=9 if failure == "unresponsive" else 3, check=False,
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
