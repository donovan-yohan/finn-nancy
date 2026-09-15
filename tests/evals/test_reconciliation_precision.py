from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.evals.fn149_corpus import render_bundle
from app.evals.reconciliation_precision import evaluate_bundle, wilson_lower_bound


BUNDLE_DIR = Path(__file__).parent / "data" / "fn149"


def _copy_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "fn149"
    shutil.copytree(BUNDLE_DIR, target)
    return target


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _refresh_manifest(root: Path, filename: str) -> None:
    manifest_path = root / "manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = (root / filename).read_bytes()
    entry = next(item for item in manifest["files"] if item["path"] == filename)
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    entry["line_count"] = payload.count(b"\n")
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_generated_bundle_is_reproducible_and_checksum_valid():
    expected = render_bundle()
    assert sorted(path.name for path in BUNDLE_DIR.iterdir()) == sorted(expected)
    for name, payload in expected.items():
        assert (BUNDLE_DIR / name).read_bytes() == payload

    report = evaluate_bundle(BUNDLE_DIR)
    assert report["ok"] is True, report["violations"]
    assert report["manifest"]["case_count"] == 1500
    assert report["manifest"]["prediction_count"] == 1500
    assert report["automation_authority"] == "disabled_foundation_only"
    assert report["controlled_modes"] == {
        "local_model": "disabled_controlled_placeholder",
        "web_search": "disabled_controlled_placeholder",
    }


def test_wilson_one_sided_lower_bound_requires_enough_zero_error_trials():
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(500, 500) < 0.995
    assert wilson_lower_bound(600, 600) > 0.995
    assert wilson_lower_bound(95, 100) == pytest.approx(0.9008389147209472)


def test_independent_metrics_and_exact_totals_pass_reference_contract():
    report = evaluate_bundle(BUNDLE_DIR)
    for claim in ("same_event", "canonical_merchant", "expense_category"):
        metric = report["metrics"][claim]
        assert metric["precision"] == 1.0
        assert metric["precision_lower_bound"] >= 0.995
        assert metric["recall"] == 1.0
        assert metric["coverage"] == 1.0
        assert metric["false_positive"] == 0
        for split in ("developer", "sealed"):
            split_metric = report["metrics_by_split"][split][claim]
            assert split_metric["precision"] == 1.0
            assert split_metric["precision_lower_bound"] >= 0.995
            assert split_metric["false_positive"] == 0
    assert report["abstention"]["rate"] == 1.0
    assert report["row_consumption"]["duplicate_row_count"] == 0
    assert report["split_leakage"] == {
        "family_overlap": [],
        "descriptor_overlap": [],
    }
    assert report["expense_totals_cents"]["delta"] == {}


def test_cli_emits_machine_readable_disabled_authority_report(tmp_path: Path):
    repo = BUNDLE_DIR.parents[3]
    output = tmp_path / "fn149-report.json"
    result = subprocess.run(
        [str(repo / "scripts" / "reconciliation-eval"), "--output", str(output)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    stdout_report = json.loads(result.stdout)
    file_report = json.loads(output.read_text(encoding="utf-8"))
    assert stdout_report == file_report
    assert stdout_report["ok"] is True
    assert stdout_report["automation_authority"] == "disabled_foundation_only"
    assert stdout_report["controlled_modes"] == {
        "local_model": "disabled_controlled_placeholder",
        "web_search": "disabled_controlled_placeholder",
    }


def test_mutation_false_automatic_decision_breaks_precision_and_abstention(
    tmp_path: Path,
):
    bundle = _copy_bundle(tmp_path)
    path = bundle / "sealed.predictions.v1.jsonl"
    rows = _jsonl(path)
    case = next(row for row in rows if row["case_id"] == "sealed-ambiguous-000")
    case["same_event"][0]["decision"] = "automatic_match"
    _write_jsonl(path, rows)
    _refresh_manifest(bundle, path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert report["metrics"]["same_event"]["false_positive"] == 1
    assert report["abstention"]["rate"] < 1.0
    assert "same_event: false automatic assignments exceed policy" in report["violations"]
    assert "sealed:same_event: false automatic assignments exceed policy" in report["violations"]
    assert "required ambiguous/unsupported abstention rate regressed" in report["violations"]


@pytest.mark.parametrize(
    ("claim_name", "case_id", "id_key", "mutated"),
    [
        (
            "same_event",
            "sealed-ambiguous-000",
            "candidate_id",
            {"decision": "automatic_match"},
        ),
        (
            "canonical_merchant",
            "sealed-exact-000",
            "subject_row_id",
            {"decision": "abstain"},
        ),
        (
            "expense_category",
            "sealed-exact-000",
            "subject_row_id",
            {"decision": "abstain"},
        ),
    ],
)
def test_mutation_duplicate_claim_ids_are_rejected(
    tmp_path: Path,
    claim_name: str,
    case_id: str,
    id_key: str,
    mutated: dict,
):
    bundle = _copy_bundle(tmp_path)
    path = bundle / "sealed.predictions.v1.jsonl"
    rows = _jsonl(path)
    case = next(row for row in rows if row["case_id"] == case_id)
    duplicate = {id_key: case[claim_name][0][id_key], **mutated}
    case[claim_name].insert(0, duplicate)
    _write_jsonl(path, rows)
    _refresh_manifest(bundle, path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert any(
        f"duplicate {claim_name} prediction id" in item
        for item in report["violations"]
    )


def test_mutation_malformed_claim_entries_are_rejected(tmp_path: Path):
    bundle = _copy_bundle(tmp_path)
    path = bundle / "sealed.predictions.v1.jsonl"
    rows = _jsonl(path)
    case = next(row for row in rows if row["case_id"] == "sealed-exact-000")
    case["same_event"].append("not-an-object")
    case["canonical_merchant"].append({"decision": "abstain"})
    _write_jsonl(path, rows)
    _refresh_manifest(bundle, path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert any("same_event prediction" in item and "must be an object" in item for item in report["violations"])
    assert any("canonical_merchant prediction" in item and "invalid subject_row_id" in item for item in report["violations"])


def test_mutation_duplicate_candidate_consumes_source_rows_twice(tmp_path: Path):
    bundle = _copy_bundle(tmp_path)
    path = bundle / "sealed.predictions.v1.jsonl"
    rows = _jsonl(path)
    case = next(row for row in rows if row["case_id"] == "sealed-duplicate-000")
    alternate = next(
        item for item in case["same_event"] if item["candidate_id"].endswith(":alternate")
    )
    alternate["decision"] = "automatic_match"
    _write_jsonl(path, rows)
    _refresh_manifest(bundle, path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert report["row_consumption"]["duplicate_row_count"] == 2
    assert "automatic event matches consume one or more source rows twice" in report["violations"]


def test_mutation_family_crossing_developer_and_sealed_is_leakage(tmp_path: Path):
    bundle = _copy_bundle(tmp_path)
    developer = _jsonl(bundle / "developer.v1.jsonl")
    path = bundle / "sealed.v1.jsonl"
    sealed = _jsonl(path)
    sealed[0]["family_id"] = developer[0]["family_id"]
    _write_jsonl(path, sealed)
    _refresh_manifest(bundle, path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert report["split_leakage"]["family_overlap"] == [developer[0]["family_id"]]
    assert any(item.startswith("family split leakage:") for item in report["violations"])


@pytest.mark.parametrize(
    ("filename", "case_id"),
    [
        ("sealed.v1.jsonl", "sealed-exact-000"),
        ("sealed.predictions.v1.jsonl", "sealed-exact-000"),
    ],
)
def test_mutation_file_case_prediction_split_identity_is_enforced(
    tmp_path: Path,
    filename: str,
    case_id: str,
):
    bundle = _copy_bundle(tmp_path)
    path = bundle / filename
    rows = _jsonl(path)
    case = next(row for row in rows if row["case_id"] == case_id)
    case["split"] = "developer"
    _write_jsonl(path, rows)
    _refresh_manifest(bundle, path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert "split does not match manifest file split" in report["violations"][0]


def test_mutation_checksum_drift_fails_before_evaluation(tmp_path: Path):
    bundle = _copy_bundle(tmp_path)
    path = bundle / "sealed.v1.jsonl"
    path.write_bytes(path.read_bytes() + b" ")

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert len(report["violations"]) == 1
    assert "checksum drift" in report["violations"][0]


def test_mutation_version_and_policy_floor_mismatch_fail_closed(tmp_path: Path):
    bundle = _copy_bundle(tmp_path)
    prediction_path = bundle / "sealed.predictions.v1.jsonl"
    rows = _jsonl(prediction_path)
    rows[0]["scorer_version"] = "unapproved-scorer"
    _write_jsonl(prediction_path, rows)
    _refresh_manifest(bundle, prediction_path.name)

    policy_path = bundle / "policy.v1.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["thresholds"]["precision_min"] = 0.99
    policy_path.write_text(
        json.dumps(policy, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_manifest(bundle, policy_path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert "policy precision_min weakens the 99.5% floor" in report["violations"]
    assert any(
        item.endswith("prediction scorer_version is not the approved version")
        for item in report["violations"]
    )


def test_mutation_policy_and_predictions_cannot_enable_live_modes(tmp_path: Path):
    bundle = _copy_bundle(tmp_path)
    live_mode = {"local_model": "live_remote", "web_search": "live"}
    for filename in (
        "developer.predictions.v1.jsonl",
        "sealed.predictions.v1.jsonl",
    ):
        path = bundle / filename
        rows = _jsonl(path)
        for row in rows:
            row["mode"] = live_mode
        _write_jsonl(path, rows)
        _refresh_manifest(bundle, filename)

    policy_path = bundle / "policy.v1.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["required_modes"] = live_mode
    policy_path.write_text(
        json.dumps(policy, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_manifest(bundle, policy_path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert "policy controlled modes must keep local model and web disabled" in report["violations"]
    assert any(item.endswith("controlled mode mismatch") for item in report["violations"])


def test_mutation_deleting_required_hard_cohort_fails_counts_and_minimums(
    tmp_path: Path,
):
    bundle = _copy_bundle(tmp_path)
    for split in ("developer", "sealed"):
        corpus_path = bundle / f"{split}.v1.jsonl"
        corpus = [row for row in _jsonl(corpus_path) if row["cohort"] != "partial"]
        _write_jsonl(corpus_path, corpus)
        _refresh_manifest(bundle, corpus_path.name)

        predictions_path = bundle / f"{split}.predictions.v1.jsonl"
        predictions = [
            row
            for row in _jsonl(predictions_path)
            if not row["case_id"].startswith(f"{split}-partial-")
        ]
        _write_jsonl(predictions_path, predictions)
        _refresh_manifest(bundle, predictions_path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert any("hard-cohort counts mismatch" in item for item in report["violations"])
    assert any("eligible decisions" in item and "below approved minimum" in item for item in report["violations"])


def test_mutation_wrong_category_breaks_precision_and_exact_totals(tmp_path: Path):
    bundle = _copy_bundle(tmp_path)
    path = bundle / "sealed.predictions.v1.jsonl"
    rows = _jsonl(path)
    case = next(row for row in rows if row["case_id"] == "sealed-exact-000")
    case["expense_category"][0]["value"] = "synthetic-category:wrong"
    _write_jsonl(path, rows)
    _refresh_manifest(bundle, path.name)

    report = evaluate_bundle(bundle)
    assert report["ok"] is False
    assert report["metrics"]["expense_category"]["false_positive"] == 1
    assert report["expense_totals_cents"]["delta"]
    assert "predicted expense category totals do not equal golden totals" in report["violations"]
