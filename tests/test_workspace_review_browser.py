"""Generated evidence anchors and accepted split contributor browser regression."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest


def test_workspace_evidence_and_category_browser_regressions():
    if not shutil.which('node'):
        pytest.skip('Node required for generated workspace browser acceptance')
    chrome = Path(os.environ.get('CHROMIUM_BIN', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
    if not chrome.exists():
        pytest.skip('Chromium required for generated workspace browser acceptance')
    result = subprocess.run(
        ['node', 'tests/browser/workspace_review.cjs'],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"anchorCases":12' in result.stdout
    assert '"contributorCases":6' in result.stdout
