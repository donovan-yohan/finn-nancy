from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def _assert_node_passed(result, *, expected: int) -> None:
    """Assert the node test runner reported the expected passes.

    The summary prefix differs between node versions ("# pass 4" versus
    "ℹ pass 4"), so match the counts rather than the decoration.
    """
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(rf"\bpass {expected}\b", result.stdout), result.stdout
    assert re.search(r"\bfail 0\b", result.stdout), result.stdout



ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _node(*args: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None, "Node.js is required to validate the capture PWA"
    return subprocess.run(
        [NODE, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_capture_javascript_parses():
    for relative_path in (
        "app/web/static/capture-store.js",
        "app/web/static/capture-outbox.js",
        "app/web/static/sw.js",
    ):
        result = _node("--check", relative_path)
        assert result.returncode == 0, result.stderr


def test_mobile_polling_and_storage_safety_guards_are_present():
    source = (ROOT / "app/web/static/capture-outbox.js").read_text()
    assert "document.hidden" in source
    assert 'document.addEventListener("visibilitychange"' in source
    assert "POLL_MAX_MS = 30_000" in source
    assert "navigator.storage.persisted" in source
    assert "navigator.storage.persist()" in source
    assert "Browser storage is not protected from eviction" in source


def test_capture_store_contract():
    result = _node("--test", "tests/js/test_capture_store.cjs")
    _assert_node_passed(result, expected=4)


def test_capture_intake_keeps_21_files_and_continues_bounded_drain():
    result = _node("--test", "tests/js/test_capture_intake.cjs")
    _assert_node_passed(result, expected=2)
