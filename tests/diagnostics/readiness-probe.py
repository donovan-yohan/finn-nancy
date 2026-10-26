#!/usr/bin/env python3
"""Observe the unchanged shipping journeys once; no intervention, retry or warmup."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

SOURCES = ["tests/js/test_ui_theme_browser.cjs", "tests/test_ui_theme_browser.py",
           "tests/test_ui_theme_browser_startup.py", ".github/workflows/ci.yml", "tests/test_ci_browser.py"]
NAMES = ["test_first_install_keeps_explicit_theme_and_control_offline_without_http_cache",
         "test_workspace_keyboard_focus_uses_accessible_theme_color[light]",
         "test_workspace_keyboard_focus_uses_accessible_theme_color[dark]"]
SCENARIOS = ["first-install-offline", "focus-light", "focus-dark"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def socket_preflight(runtime):
    """Check the longest selected pytest basename, including pytest's current suffix."""
    # pytest's makeNumberedDir uses a 30-character sanitized test name + counter.
    longest = max(len("".join(c if c.isalnum() or c in "_-." else "_" for c in name)[:30])
                  for name in NAMES)
    pathname = runtime / ("x" * longest + "0") / "com.google.Chrome.XXXXXX" / "SingletonSocket"
    count = len(os.fsencode(pathname))
    if sys.platform != "linux":
        return {"predictedBytes": count, "linuxBind": "not-applicable"}
    if count >= 108:
        raise ValueError(f"generated Linux singleton socket path is {count} bytes (must be <108)")
    pathname.parent.mkdir(parents=True)
    try:
        with socket.socket(socket.AF_UNIX) as control:
            control.bind(str(pathname))
    finally:
        pathname.unlink(missing_ok=True)
        pathname.parent.rmdir()
        pathname.parent.parent.rmdir()
    return {"predictedBytes": count, "linuxBind": "passed"}


class Observe:
    def __init__(self, root, output, observer):
        self.root, self.output, self.observer = root, output, observer
        self.records = []
        self.module = self.original = None

    def pytest_collection_modifyitems(self, items):
        assert [item.name for item in items] == NAMES
        self.module = items[0].module
        self.original = self.module.subprocess
        # Patch only this test module. Keep the shipping Linux Popen/owned-group path.
        self.module.subprocess = SimpleNamespace(
            Popen=self.popen, run=self.run, PIPE=subprocess.PIPE,
            CompletedProcess=subprocess.CompletedProcess, TimeoutExpired=subprocess.TimeoutExpired,
        )

    def observe(self, command, kwargs, launch):
        argv = list(command)
        index = next(i for i, value in enumerate(argv)
                     if Path(value) == self.root / "tests/js/test_ui_theme_browser.cjs")
        assert Path(argv[index - 1]).name == "node"
        if sys.platform == "linux":
            assert index == 3 and Path(argv[0]).name == "dbus-run-session" and argv[1] == "--"
            assert kwargs["start_new_session"] is True
        else:
            assert index == 1
        scenario = argv[-1]
        assert scenario == SCENARIOS[len(self.records)]
        directory = self.output / scenario
        directory.mkdir(mode=0o700)
        environment = dict(kwargs["env"])
        assert not {"NODE_OPTIONS", "DBUS_SESSION_BUS_ADDRESS", "FN_STARTUP_OBSERVATION"} & environment.keys()
        environment["FN_STARTUP_OBSERVATION"] = str(directory / "startup.json")
        instrumented = [*argv[:index], "--require", str(self.observer), *argv[index:]]
        record = {"scenario": scenario, "originalArgv": argv, "observedArgv": instrumented,
                  "environmentKeys": sorted(environment), "profile": argv[-2], "started": time.monotonic()}
        self.records.append(record)
        result = launch(instrumented, **{**kwargs, "env": environment})
        record["pid"] = getattr(result, "pid", None)
        return result

    def popen(self, argv, **kwargs):
        return self.observe(argv, kwargs, subprocess.Popen)

    def run(self, argv, **kwargs):
        return self.observe(argv, kwargs, subprocess.run)

    def restore(self):
        if self.module is not None:
            self.module.subprocess = self.original

    def retain(self):
        for record in self.records:
            directory = self.output / record["scenario"]
            stderr = Path(record["profile"] + ".stderr.log")
            if stderr.is_file():
                shutil.copyfile(stderr, directory / "chrome.stderr.log")
                (directory / "chrome.stderr.log").chmod(0o600)
            record["observationExists"] = (directory / "startup.json").is_file()
        (self.output / "commands.json").write_text(json.dumps(self.records, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "CHROMIUM_BIN" not in os.environ:
        parser.error("explicit CHROMIUM_BIN is required")
    root, output = args.root.resolve(), args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    observer = Path(__file__).with_name("readiness-observer.cjs").resolve()
    source = {name: digest(root / name) for name in SOURCES}
    runtime = Path(tempfile.mkdtemp(prefix="c", dir=os.environ["TMPDIR"]))
    try:
        preflight = socket_preflight(runtime)
    except BaseException:
        shutil.rmtree(runtime)
        raise
    identity = {"source": source, "probeSha256": digest(Path(__file__)), "observerSha256": digest(observer),
                "platform": sys.platform, "kernel": os.uname().release, "python": sys.version,
                "socketPreflight": preflight, "runtime": str(runtime),
                "browser": os.environ.get("CHROMIUM_BIN"),
                "resolvedBrowser": os.path.realpath(os.environ["CHROMIUM_BIN"]),
                "toolPaths": {name: shutil.which(name) for name in ("node", "dbus-run-session", "strace", "perf")}}
    policy = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    identity["apparmorRestriction"] = policy.read_text().strip() if policy.exists() else None
    for label, command in [("head", ["git", "rev-parse", "HEAD"]),
                           ("tree", ["git", "rev-parse", "HEAD^{tree}"])]:
        identity[label] = subprocess.run(command, cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    (output / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    sys.path.insert(0, str(root))
    os.chdir(root)
    import pytest
    plugin = Observe(root, output, observer)
    try:
        status = int(pytest.main(["-q", "-rA", "-s", "tests/test_ui_theme_browser.py",
                                 "--basetemp=" + str(runtime),
                                 "--junitxml=" + str(output / "junit.xml")], plugins=[plugin]))
    finally:
        plugin.restore()
        plugin.retain()
        unchanged = all(digest(root / name) == sha for name, sha in source.items())
        (output / "source-unchanged.json").write_text(json.dumps({"unchanged": unchanged}) + "\n")
        shutil.rmtree(runtime)
    assert unchanged
    assert [row["scenario"] for row in plugin.records] == SCENARIOS
    assert all(row["observationExists"] for row in plugin.records)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
