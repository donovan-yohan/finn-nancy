from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from . import create_agent_tools, deep_agent_filesystem, hitl, persistence, streaming, structured_output, subagent_same_model
from .helpers import curl_models_preflight, env_summary, warm_pinned_model
from .results import SmokeResult

PrimitiveFn = Callable[[], SmokeResult]

PRIMITIVES: list[PrimitiveFn] = [
    create_agent_tools.run,
    structured_output.run,
    deep_agent_filesystem.run,
    subagent_same_model.run,
    persistence.run,
    streaming.run,
    hitl.run,
]


def run_all(*, do_preflight: bool = True, do_warmup: bool = True) -> list[SmokeResult]:
    print(f"Deep Agents compatibility smoke: {env_summary()}")
    if do_preflight:
        ok, note = curl_models_preflight()
        print(f"preflight {'PASS' if ok else 'BLOCKED'} - {note}")
        if not ok:
            return [SmokeResult("preflight /models", "BLOCKED", note)]
    if do_warmup:
        ok = warm_pinned_model()
        print(f"warmup {'PASS' if ok else 'BLOCKED'} - tiny qwen3.8-27b-unleashed call")
        if not ok:
            return [SmokeResult("warmup", "BLOCKED", "warm() returned False")]

    results: list[SmokeResult] = []
    for fn in PRIMITIVES:
        result = fn()
        results.append(result)
        print(f"{result.primitive}: {result.status} - {result.note}")
        for detail in result.details:
            print(f"  observed: {detail}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live Mistral/Deep Agents compatibility smokes.")
    parser.add_argument("--json-out", type=Path, help="Optional path for machine-readable results.")
    parser.add_argument("--no-preflight", action="store_true", help="Skip /models curl preflight.")
    parser.add_argument("--no-warmup", action="store_true", help="Skip tiny warmup call.")
    args = parser.parse_args()

    results = run_all(do_preflight=not args.no_preflight, do_warmup=not args.no_warmup)
    if args.json_out:
        args.json_out.write_text(
            json.dumps([result.to_dict() for result in results], indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.json_out}")

    return 1 if any(result.status in {"BLOCKED", "FAIL"} for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
