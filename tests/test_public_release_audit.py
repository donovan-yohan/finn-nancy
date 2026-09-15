from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "public_release_audit", str(REPO / "scripts" / "public-release-audit")
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(MODULE)


def _write_manifest(
    root: Path,
    *,
    fixtures: dict[str, str] | None = None,
    binary_assets: dict[str, str] | None = None,
) -> None:
    (root / "PUBLIC_RELEASE_MANIFEST.json").write_text(
        json.dumps(
            {
                "schema": MODULE.RELEASE_MANIFEST_SCHEMA,
                "declaration": MODULE.RELEASE_DECLARATION,
                "fixtures": fixtures or {},
                "binary_assets": binary_assets or {},
            }
        )
    )


def test_public_release_audit_accepts_the_candidate_tree():
    assert MODULE.audit(REPO) == []


def test_public_release_audit_rejects_sensitive_release_content(tmp_path):
    _write_manifest(tmp_path)
    (tmp_path / "README.md").write_text(
        "contact " + "operator" + "@not-example.test\n"
    )
    (tmp_path / ".env").write_text("API_TOKEN=not-a-placeholder\n")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "ledger.json").write_text('{"merchant": "Example Shop"}\n')

    failures = MODULE.audit(tmp_path)

    assert any("non-placeholder email" in item for item in failures)
    assert any("sensitive file extension" in item for item in failures)
    assert any("unreviewed synthetic fixture" in item for item in failures)


def test_public_release_audit_allows_documentation_and_manifested_fixture(tmp_path):
    (tmp_path / "README.md").write_text(
        "service@example.invalid uses "
        + "192."
        + "0.2.10 and https://example.invalid for documentation\n"
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fixture = fixtures / "ledger.json"
    fixture.write_text('{"merchant": "Synthetic Shop"}\n')
    _write_manifest(
        tmp_path,
        fixtures={"fixtures/ledger.json": hashlib.sha256(fixture.read_bytes()).hexdigest()},
    )

    assert MODULE.audit(tmp_path) == []


def test_public_release_audit_accepts_manifested_fixture_and_rejects_hash_drift(tmp_path):
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    fixture = fixtures / "ledger.json"
    fixture.write_text('{"merchant": "Synthetic Shop"}\n')
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    _write_manifest(tmp_path, fixtures={"tests/fixtures/ledger.json": digest})

    assert MODULE.audit(tmp_path) == []

    fixture.write_text('{"merchant": "Changed Shop"}\n')
    assert any("manifest hash mismatch" in item for item in MODULE.audit(tmp_path))


def test_public_release_audit_rejects_untracked_files_in_git_checkout(tmp_path):
    _write_manifest(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "PUBLIC_RELEASE_MANIFEST.json"],
        check=True,
    )
    (tmp_path / "untracked.txt").write_text(
        "contact " + "operator" + "@not-example.test\n"
    )

    assert any("non-placeholder email" in item for item in MODULE.audit(tmp_path))


def test_public_release_audit_rejects_partial_redactions_and_private_topology(tmp_path):
    _write_manifest(tmp_path)
    (tmp_path / "README.md").write_text(
        "owner=sample_member_a" + "zzz\n"
        "endpoint=http://" + "build-box.internal:8771\n"
        "storage=/" + "srv/private-ledger/data.sqlite\n"
    )

    failures = MODULE.audit(tmp_path)

    assert any("malformed synthetic-person placeholder" in item for item in failures)
    assert any("private or single-label hostname" in item for item in failures)
    assert any("personal absolute path" in item for item in failures)


def test_public_release_audit_rejects_secret_names_and_credential_shapes(tmp_path):
    _write_manifest(tmp_path)
    (tmp_path / ".env.example").write_text(
        "LOCAL_LLM_KEY=" + "not-a-real-but-populated-value\n"
    )
    (tmp_path / "README.md").write_text(
        "credential=" + "ghp_" + "A" * 24 + "\n"
    )

    failures = MODULE.audit(tmp_path)

    assert any("populated secret assignment" in item for item in failures)
    assert any("credential-shaped value" in item for item in failures)


def test_public_release_audit_rejects_dotenv_systemd_and_tailnet_bypasses(tmp_path):
    _write_manifest(tmp_path)
    (tmp_path / ".env.production").write_text(
        "DB_" + "PASSWORD=not-a-placeholder\n"
    )
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "app.service").write_text(
        "Environment=API_" + "KEY=live-value-123\n"
    )
    (tmp_path / "README.md").write_text(
        "endpoint 100." + "101.102.103\n"
    )

    failures = MODULE.audit(tmp_path)

    assert any(".env.production: sensitive file extension" in item for item in failures)
    assert any("deploy/app.service: populated secret assignment" in item for item in failures)
    assert any("README.md: private IP address" in item for item in failures)
