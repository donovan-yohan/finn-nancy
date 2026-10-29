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
BROWSER_MODULES = [
    "tests/test_ui_theme_browser.py",
    "tests/test_design_banking.py",
    "tests/test_design_shell.py",
]


@pytest.mark.parametrize(("scenario", "exit_code"), [
    ("packaged", 0),
    ("missing", 1),
    ("nonexecutable", 1),
    ("version-failure", 37),
    ("general-failure", 41),
    ("browser-failure", 43),
])
def test_ci_selects_packaged_chrome_only_for_mandatory_browser_modules(
    tmp_path, scenario, exit_code,
):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step = workflow.split("      - name: Test\n", 1)[1].split("\n      - name:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    assert "CHROMIUM_BIN=/usr/bin/google-chrome" in script, (
        "Mandatory browser modules must explicitly select the runner-packaged Chrome"
    )
    for forbidden in ("--no-sandbox", "--disable-setuid-sandbox", "sudo", "sysctl"):
        assert forbidden not in script

    # Execute the real workflow shell with generated commands, never a real browser.
    # Substitute only the system browser path; do not recreate its selection logic.
    chrome = tmp_path / "google-chrome"
    if scenario != "missing":
        chrome.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            "print(json.dumps({'browser_args': sys.argv[1:]}))\n"
            f"sys.exit({37 if scenario == 'version-failure' else 0})\n"
        )
        chrome.chmod(0o600 if scenario == "nonexecutable" else 0o700)
    script = script.replace("/usr/bin/google-chrome", str(chrome))
    # A following command must not hide a failed browser-phase command.
    script += "\nprintf '%s\\n' '{\"completed\": true}'\n"
    commands = tmp_path / "bin"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\\n' '{\"diagnostic\": true}'\n")
    uv.chmod(0o700)
    tests = tmp_path / "scripts/test"
    tests.parent.mkdir()
    tests.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "print(json.dumps({'tests': sys.argv[1:], "
        "'browser': os.environ.get('CHROMIUM_BIN')}))\n"
        f"general_exit = {41 if scenario == 'general-failure' else 0}\n"
        f"browser_exit = {43 if scenario == 'browser-failure' else 0}\n"
        "sys.exit(general_exit if sys.argv[1].startswith('--ignore=') else browser_exit)\n"
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
    assert result.returncode == exit_code, result.stdout + result.stderr
    expected = [{
        "tests": [f"--ignore={module}" for module in BROWSER_MODULES], "browser": None,
    }]
    if scenario in {"packaged", "version-failure", "browser-failure"}:
        expected.append({"browser_args": ["--version"]})
    if scenario in {"packaged", "browser-failure"}:
        expected.extend([
            {"diagnostic": True},
            {"tests": BROWSER_MODULES, "browser": str(chrome)},
        ])
    if scenario == "packaged":
        expected.append({"completed": True})
    assert records == expected, "Keep all three modules in the scoped phase and fail closed"
