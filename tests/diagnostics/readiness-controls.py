#!/usr/bin/env python3
"""Generated collector controls. No Chrome, accounts, network egress or dependencies."""
from __future__ import annotations

import importlib.util
import ast
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    node = shutil.which("node")
    assert node
    generated = Path(tempfile.mkdtemp(prefix="rc", dir=os.environ["TMPDIR"]))
    records = []
    try:
        spec = importlib.util.spec_from_file_location("probe", HERE / "readiness-probe.py")
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        if sys.platform == "linux":
            # Socket guard's exact accepted/refused byte boundary and UTF-8 case.
            for length, accepted in [(107, True), (108, False)]:
                suffix = "/" + "x" * 31 + "/com.google.Chrome.XXXXXX/SingletonSocket"
                runtime = generated / ("x" * (length - len(os.fsencode(generated)) - 1 - len(suffix)))
                runtime.mkdir()
                try:
                    result = probe.socket_preflight(runtime)
                    assert accepted and result["predictedBytes"] == length
                except ValueError:
                    assert not accepted
            runtime = generated / ("é" * 20 + "x" * 25)
            runtime.mkdir()
            try:
                probe.socket_preflight(runtime)
                raise AssertionError("multibyte overflowing path must be refused")
            except ValueError:
                pass
        # Qualify mapping classification without a raw maps/profile upload.
        parser = subprocess.run([node, "-e", "const assert=require('node:assert');"
            f"const p=require({str(HERE / 'readiness-observer.cjs')!r});"
            "const maps='1000-2000 r-xp 00001000 00:00 1 /opt/google/chrome/chrome';"
            "assert.deepStrictEqual(p.instructionMapping('-1 0x0 0x1234',maps),"
            "{class:'browser-install',basename:'chrome',permissions:'r-xp',fileOffset:'0x1234'});"
            "assert.strictEqual(p.instructionMapping('running',maps),null);"
            "assert.strictEqual(p.classify('/unrelated/private/path'),'other-redacted');"],
            capture_output=True, text=True, check=True)
        assert not parser.stderr
        # Exercise the actual helper through the probe's module-local interception,
        # including Linux's existing owned-session Popen instead of replacing it.
        source = ROOT / "tests/test_ui_theme_browser.py"
        function = next(n for n in ast.parse(source.read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == "run_browser")
        scope = {"shutil": shutil, "os": os, "subprocess": subprocess, "sys": sys,
                 "Path": Path, "ROOT": ROOT, "signal": signal}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
        module = SimpleNamespace(subprocess=subprocess)
        output = generated / "composition"
        output.mkdir()
        plugin = probe.Observe(ROOT, output, HERE / "readiness-observer.cjs")
        plugin.pytest_collection_modifyitems([SimpleNamespace(name=name, module=module) for name in probe.NAMES])
        boundary_browser = generated / "composition-browser"
        boundary_browser.write_text(f"#!{node}\nprocess.exit(23);\n")
        boundary_browser.chmod(0o700)
        old_chrome = os.environ.get("CHROMIUM_BIN")
        os.environ["CHROMIUM_BIN"] = str(boundary_browser)
        scope["subprocess"] = module.subprocess
        try:
            for scenario in probe.SCENARIOS:
                directory = generated / ("composition-" + scenario)
                try:
                    scope["run_browser"](directory / "site", directory, scenario)
                    raise AssertionError("generated nonzero child must fail")
                except AssertionError as error:
                    assert "code=23" in str(error), error
            plugin.retain()
            assert all(row["observationExists"] for row in plugin.records)
        finally:
            plugin.restore()
            assert module.subprocess is subprocess
            if old_chrome is None:
                os.environ.pop("CHROMIUM_BIN", None)
            else:
                os.environ["CHROMIUM_BIN"] = old_chrome
        for mode in ["delayed-listener", "delayed-headers", "delayed-body", "empty-then-page", "empty-until-deadline",
                     "stalled-headers", "stalled-body", "no-listener", "early-exit", "signal", "ignored-term"]:
            directory = generated / mode
            directory.mkdir()
            browser = directory / "browser"
            pid_file = directory / "pid"
            observation = directory / "startup.json"
            browser.write_text(f"#!{node}\n"
                "const fs=require('node:fs'),http=require('node:http');\n"
                f"const mode={mode!r};fs.writeFileSync({str(pid_file)!r},String(process.pid));\n"
                "if(mode==='early-exit'){fs.writeSync(2,'generated early exit\\n');process.exit(23);}\n"
                "if(mode==='signal'){process.kill(process.pid,'SIGTERM');}\n"
                "const port=Number(process.argv.find(a=>a.startsWith('--remote-debugging-port=')).split('=')[1]);\n"
                "const body=JSON.stringify([{type:'page',webSocketDebuggerUrl:'ws://127.0.0.1:'+port+'/generated'}]);\n"
                "let count=0;const server=http.createServer((req,res)=>{\n"
                "if(req.url!=='/json/list')throw Error('unexpected request');\n"
                "if(mode==='stalled-headers')return;\n"
                "if(mode==='empty-until-deadline'){res.end('[]');return;}\n"
                "if(mode==='delayed-listener'){setTimeout(()=>res.end(body),40);return;}\n"
                "if(mode==='stalled-body'){res.writeHead(200);res.flushHeaders();res.write('[');return;}\n"
                "if(mode==='delayed-headers'){setTimeout(()=>res.end(body),200);return;}\n"
                "if(mode==='delayed-body'){res.writeHead(200);res.flushHeaders();setTimeout(()=>res.end(body),200);return;}\n"
                "if(mode==='empty-then-page'&&count++===0){res.end('[]');return;}res.end(body);});\n"
                "if(mode==='ignored-term')process.on('SIGTERM',()=>{});\n"
                "if(['no-listener','ignored-term'].includes(mode)){setInterval(()=>{},1000);}\n"
                "else setTimeout(()=>server.listen(port,'127.0.0.1',()=>{\n"
                "fs.writeSync(2,'DevTools listening ');setTimeout(()=>fs.writeSync(2,'on ws://generated\\n'),10);\n"
                "}),mode==='delayed-listener'?200:0);\n")
            browser.chmod(0o700)
            handoff = directory / "handoff.cjs"
            handoff.write_text("global.WebSocket=class {constructor(){throw Error('GENERATED PAGE HANDOFF');}};\n")
            env = {"PATH": os.environ.get("PATH", ""), "TMPDIR": str(directory),
                   "CHROMIUM_BIN": str(browser), "FN_STARTUP_OBSERVATION": str(observation)}
            command = [node, "--require", str(handoff), "--require", str(HERE / "readiness-observer.cjs"),
                       str(ROOT / "tests/js/test_ui_theme_browser.cjs"), str(directory / "site"),
                       str(directory / "chrome"), "focus-light"]
            if sys.platform == "linux":
                command = [shutil.which("dbus-run-session"), "--", *command]
            start = time.monotonic()
            process = subprocess.Popen(command, env=env, cwd=directory, start_new_session=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert process.stdout is not None and process.stderr is not None
            try:
                stdout, stderr = process.communicate(timeout=9)
                elapsed = time.monotonic() - start
                assert process.returncode == 1, (mode, stdout, stderr)
                data = json.loads(observation.read_text())
                if mode != "empty-until-deadline":
                    assert data["droppedEvents"] == 0
                events = data["events"]
                kinds = [event["kind"] for event in events]
                assert len(events) <= 512
                assert kinds.count("readiness-boundary-snapshot") == 1, (mode, len(events), data["droppedEvents"])
                assert kinds.count("observer-exit") == 1, (mode, len(events), data["droppedEvents"])
                assert events == sorted(events, key=lambda e: e["elapsedMs"])
                assert all(e.get("requestId") is not None for e in events
                           if e["kind"] in ["http-request-created", "http-request-send-headers", "http-response-headers"])
                snapshot = next(e for e in events if e["kind"] == "readiness-boundary-snapshot")
                if mode.startswith("delayed") or mode == "empty-then-page":
                    assert "GENERATED PAGE HANDOFF" in stderr and "startup deadline" not in stderr, (mode, stderr)
                    assert snapshot["boundary"] == "page-target-ready"
                    for kind in ["http-client-connected", "http-request-send-headers", "http-response-headers",
                                 "readiness-headers", "readiness-body", "page-websocket-handoff"]:
                        assert kind in kinds, (mode, kind)
                    headers = next(e["elapsedMs"] for e in events if e["kind"] == "readiness-headers")
                    body = next(e["elapsedMs"] for e in events if e["kind"] == "readiness-body")
                    if mode == "delayed-body":
                        assert body - headers >= 150
                    if mode == "delayed-headers":
                        sent = next(e["elapsedMs"] for e in events if e["kind"] == "http-request-send-headers")
                        assert headers - sent >= 150
                    if mode == "empty-then-page":
                        assert [e["targetTypes"] for e in events if e["kind"] == "readiness-body"] == [[], ["page"]]
                    if mode == "delayed-listener":
                        assert "listener-announcement-observed" in kinds
                        assert any(e.get("causeCode") == "ECONNREFUSED" for e in events)
                else:
                    assert "page-websocket-handoff" not in kinds
                    assert snapshot["boundary"] == "startup-error-before-cleanup"
                    if mode in ["early-exit", "signal"]:
                        assert elapsed < 3
                        assert ("code=23" if mode == "early-exit" else "signal=SIGTERM") in stderr
                    else:
                        assert "startup deadline exceeded" in stderr and 5 <= elapsed < 8
                    if mode == "stalled-headers":
                        assert "http-request-send-headers" in kinds and "readiness-headers" not in kinds
                    if mode == "stalled-body":
                        assert "readiness-headers" in kinds and "readiness-body-error" in kinds
                    if mode in ["no-listener", "ignored-term"]:
                        assert "http-client-connected" not in kinds
                assert next(e for e in events if e["kind"] == "spawn-requested")["stderrStdioPreserved"]
                pid = int(pid_file.read_text())
                try:
                    os.kill(pid, 0)
                    raise AssertionError("generated browser survived launcher cleanup")
                except ProcessLookupError:
                    pass
                if sys.platform == "linux":
                    assert snapshot["processes"]
                records.append({"mode": mode, "returncode": process.returncode, "elapsedSeconds": elapsed,
                                "browserReaped": True, "observation": data})
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                process.stdout.close()
                process.stderr.close()
        print(json.dumps({"classification": "generated observer controls, not Chrome or hosted causality",
                          "platform": sys.platform, "node": subprocess.check_output([node, "--version"], text=True).strip(),
                          "socketBoundary": "passed" if sys.platform == "linux" else "Linux-only",
                          "exactHelperComposition": {"cases": len(plugin.records), "nonzeroPropagated": True},
                          "records": records}, indent=2))
    finally:
        shutil.rmtree(generated)


if __name__ == "__main__":
    main()
