"""Rendered onboarding acceptance, using only isolated generated fixture servers."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_onboarding_cdp_keyboard_and_timeout_contract():
    if not shutil.which("node"):
        pytest.skip("Node required for CDP harness checks")
    result = subprocess.run(
        ["node", "--test", "tests/js/plaid_onboarding_cdp.test.cjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("journey", [
    "first", "accounts", "drafts", "identity", "keyboard", "lifecycle", "repair-exit", "repair-success",
    "sync-focus", "manual", "exchange", "revocation", "empty-disconnected", "empty-unconfirmed",
    "empty-disabled", "empty-login", "empty-healthy",
])
def test_visible_onboarding_journey(journey):
    if not shutil.which("node"):
        pytest.skip("Node required for rendered onboarding")
    chrome = os.environ.get("CHROMIUM_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not Path(chrome).exists():
        pytest.skip("Chromium required for rendered onboarding")
    result = subprocess.run(["node", "tests/browser/plaid_onboarding.cjs", journey], cwd=ROOT,
                            capture_output=True, text=True, timeout=100)
    assert result.returncode == 0, result.stdout + result.stderr
