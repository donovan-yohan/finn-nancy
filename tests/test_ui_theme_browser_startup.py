"""Exercise the real Node launcher with generated children, never a user browser."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/js/test_ui_theme_browser.cjs"


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
