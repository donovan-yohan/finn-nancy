from __future__ import annotations

import pytest


@pytest.mark.llm
def test_deepagents_compat_smoke_runner_executes_all_primitives():
    from spikes.deepagents_compat.run import PRIMITIVES, run_all

    results = run_all()

    assert len(results) == len(PRIMITIVES)
    assert {result.status for result in results} <= {"PASS", "NEEDS_ADAPTER", "BLOCKED", "FAIL"}
