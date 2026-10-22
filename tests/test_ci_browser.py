"""Exercise CI browser selection without launching a browser or changing policy."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("browser_present", [True, False], ids=["packaged", "missing"])
def test_ci_selects_packaged_chrome_only_for_theme_scenarios(tmp_path, browser_present):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step = workflow.split("      - name: Test\n", 1)[1].split("\n      - name:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    assert "CHROMIUM_BIN=/usr/bin/google-chrome" in script, (
        "Theme scenarios must explicitly select the runner-packaged Chrome"
    )
    for forbidden in ("--no-sandbox", "--disable-setuid-sandbox", "sudo", "sysctl"):
        assert forbidden not in script

    # Execute the real workflow shell with generated commands, never a real browser.
    # Substitute only the system browser path; do not recreate its selection logic.
    chrome = tmp_path / "google-chrome"
    if browser_present:
        chrome.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            "print(json.dumps({'browser_args': sys.argv[1:]}))\n"
        )
        chrome.chmod(0o700)
    script = script.replace("/usr/bin/google-chrome", str(chrome))
    commands = tmp_path / "bin"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o700)
    tests = tmp_path / "scripts/test"
    tests.parent.mkdir()
    tests.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "print(json.dumps({'tests': sys.argv[1:], "
        "'browser': os.environ.get('CHROMIUM_BIN')}))\n"
    )
    tests.chmod(0o700)
    bash = shutil.which("bash")
    assert bash, "Bash is required to exercise the CI test step"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={"PATH": str(commands) + os.pathsep + os.defpath, "TMPDIR": str(tmp_path)},
        text=True, capture_output=True, timeout=10, check=False,
    )
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    invocations = [record for record in records if "tests" in record]
    assert invocations[0] == {
        "tests": ["--ignore=tests/test_ui_theme_browser.py"], "browser": None,
    }
    if browser_present:
        assert result.returncode == 0, result.stdout + result.stderr
        assert records.count({"browser_args": ["--version"]}) == 1
        assert invocations == [invocations[0], {
            "tests": ["tests/test_ui_theme_browser.py"], "browser": str(chrome),
        }]
    else:
        assert result.returncode != 0, "Missing packaged Chrome must fail, not fall back"
        assert len(invocations) == 1, "Theme tests must not run with a fallback browser"
