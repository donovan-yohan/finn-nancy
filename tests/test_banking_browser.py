"""Composed household banking journey with generated provider fixtures only."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_banking_cdp_teardown_contract():
    if not shutil.which('node'):
        pytest.skip('Node required for banking CDP harness checks')
    result = subprocess.run(['node', '--test', 'tests/js/banking_cdp.test.cjs'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_two_member_banking_browser_journey():
    if not shutil.which('node'):
        pytest.skip('Node required for generated banking browser acceptance')
    chrome = Path(os.environ.get('CHROMIUM_BIN', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
    if not chrome.exists():
        pytest.skip('Chromium required for generated banking browser acceptance')
    result = subprocess.run(
        ['node', 'tests/browser/banking.cjs'], cwd=ROOT,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"passed":7' in result.stdout
