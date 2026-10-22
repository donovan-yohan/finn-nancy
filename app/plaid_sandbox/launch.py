"""Launch a fixed private candidate without loading operator configuration."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def candidate_root():
    return Path(__file__).resolve().parents[2] / "data" / "plaid-sandbox"


def clean_environment():
    return {"PATH": os.defpath, "STRICT_LOCAL_MODE": "true", "PYTHONUNBUFFERED": "1",
            "TMPDIR": str(candidate_root().parent / "sandbox-runtime-tmp")}


def main():
    # Re-exec first, before importing a settings loader or server dependency.
    if os.environ.get("FN_SANDBOX_CLEAN_PROCESS") != "1":
        env = clean_environment()
        env["FN_SANDBOX_CLEAN_PROCESS"] = "1"
        os.execve(sys.executable, [sys.executable, "-m", "app.plaid_sandbox.launch", *sys.argv[1:]], env)
    # The marker is not authority: scrub even when a caller supplies it.
    env = clean_environment()
    env["FN_SANDBOX_CLEAN_PROCESS"] = "1"
    os.environ.clear()
    os.environ.update(env)
    parser = argparse.ArgumentParser(description="Isolated Plaid Sandbox candidate; loopback only, no real bank accounts.")
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--credentials-file", type=Path, help="Explicit operator-provisioned private Sandbox JSON handoff; contents load only after local consent")
    parser.add_argument("--check", action="store_true", help="Report only redacted candidate status; do not serve")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    root = candidate_root()
    # Refuse redirected/symlinked paths instead of wandering into operator data.
    if root.parent.is_symlink() or root.is_symlink():
        parser.error("candidate data paths must not be symlinks")
    from .web import create_app
    app = create_app(root, port=args.port, credentials_file=args.credentials_file)
    if args.check:
        print(json.dumps({**app.state.store.status(), "bind": f"127.0.0.1:{args.port}", "strict_local_mode": True,
                          "other_transports": False, "ledger_authority": False}))
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
