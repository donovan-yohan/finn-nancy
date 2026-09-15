from __future__ import annotations

import ast
import builtins
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest

import app.evals.fn149b_controlled_scorer as controlled_scorer_module
import app.evals.fn149b_descriptor_precision as descriptor_evaluator_module
from app.evals.fn149b_controlled_scorer import (
    CONTROLLED_SCORER_ARTIFACT_DIGEST,
    controlled_reference_predictions,
)
from app.evals.fn149b_descriptor_corpus import (
    CONTROLLED_SCORER_VERSION,
    SCORER_INPUT_KEYS,
    PacketError,
    canonical_json_bytes,
    load_packet,
    sanitize_scorer_input,
    scorer_inputs,
)
from app.evals.fn149b_descriptor_precision import (
    evaluate_packet,
    evaluate_predictions,
)


PACKET_DIR = Path(__file__).parents[1] / "fixtures" / "fn149b"


def _copy_packet(tmp_path: Path) -> Path:
    target = tmp_path / "fn149b"
    shutil.copytree(PACKET_DIR, target)
    return target


def _refresh_manifest(root: Path, filename: str) -> None:
    manifest_path = root / "manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = (root / filename).read_bytes()
    entry = next(row for row in manifest["files"] if row["path"] == filename)
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _case_index(packet, *, split: str, cohort: str) -> int:
    return next(
        index
        for index, case in enumerate(packet.public_cases)
        if case["split"] == split and case["cohort"] == cohort
    )


def _controlled(packet):
    return controlled_reference_predictions(
        scorer_inputs(packet),
        packet.knowledge,
        corpus_digest=packet.corpus_digest,
        knowledge_digest=packet.knowledge_digest,
        manifest_digest=packet.manifest_digest,
        policy_digest=packet.policy_digest,
    )


def test_sealed_packet_is_deterministic_private_and_gold_separated():
    first = load_packet(PACKET_DIR)
    second = load_packet(PACKET_DIR)

    assert first.identity == second.identity
    assert first.public_cases == second.public_cases
    assert first.gold_rows == second.gold_rows
    assert len(first.public_cases) == 1560
    assert len(first.gold_rows) == 1560
    assert {
        split: sum(case["split"] == split for case in first.public_cases)
        for split in ("developer", "sealed")
    } == {"developer": 780, "sealed": 780}

    sanitized = scorer_inputs(first)
    assert all(set(row) == SCORER_INPUT_KEYS for row in sanitized)
    assert all(row["descriptor"].startswith("SYNTH ") for row in sanitized)
    assert all(
        row["scope"]["account_id"].startswith("synthetic-account:")
        for row in sanitized
    )
    assert all(
        not ({"gold", "cohort", "family_id", "template_id"} & set(row))
        for row in sanitized
    )
    assert not any(
        "target_id" in json.dumps(row, sort_keys=True) for row in sanitized
    )
    assert {
        row["case_id"] for row in sanitized
    } == {row["case_id"] for row in first.gold_rows}


def test_controlled_reference_passes_metrics_but_never_automation():
    report = evaluate_packet(PACKET_DIR)

    assert report["ok"] is True, report["violations"]
    assert report["evidence_class"] == "controlled_reference"
    for claim_name in ("merchant", "category"):
        assert report["metrics"][claim_name]["precision"] == 1.0
        assert report["metrics"][claim_name]["coverage"] == 1.0
        assert report["metrics"][claim_name]["correct_coverage"] == 1.0
        assert report["metrics"][claim_name]["false_positive"] == 0
        for split in ("developer", "sealed"):
            metric = report["metrics_by_split"][split][claim_name]
            assert metric["eligible"] == 630
            assert metric["precision_lower_bound"] >= 0.995
    assert all(
        cohort["rate"] == 1.0
        for claims in report["abstention_by_cohort"].values()
        for cohort in claims.values()
    )
    receipt = report["automation_receipt"]
    assert receipt["enabled"] is False
    assert receipt["authority"] == "disabled_fail_closed"
    assert "scorer_kind_is_not_production" in receipt["blockers"]
    assert "controlled_reference_version_has_no_authority" in receipt["blockers"]
    assert "controlled_reference_artifact_has_no_authority" in receipt["blockers"]
    assert "approved_production_tuple_missing" in receipt["blockers"]
    assert (
        "independently_verifiable_production_run_receipt_missing"
        in receipt["blockers"]
    )
    for split in ("developer", "sealed"):
        for claim_name in ("merchant", "category"):
            cluster = report["independent_cluster_metrics"][split][claim_name]
            assert cluster["trial_unit"] == "independent_descriptor_template"
            assert cluster["eligible"] == 7
            assert cluster["successful"] == 7
            assert cluster["precision_lower_bound"] < 0.995
            assert cluster["sufficient_for_production"] is False


def test_independent_claim_resolution_is_visible_in_cases_and_metrics():
    packet = load_packet(PACKET_DIR)
    predictions = _controlled(packet)

    merchant_only = _case_index(
        packet,
        split="sealed",
        cohort="merchant_only_confirmed",
    )
    category_only = _case_index(
        packet,
        split="sealed",
        cohort="category_only_confirmed",
    )
    assert predictions[merchant_only]["merchant"]["status"] == "resolved"
    assert (
        predictions[merchant_only]["merchant"]["automatic_assignment_allowed"]
        is True
    )
    assert predictions[merchant_only]["category"]["status"] == "abstained"
    assert (
        predictions[merchant_only]["category"]["automatic_assignment_allowed"]
        is False
    )
    assert predictions[category_only]["merchant"]["status"] == "abstained"
    assert predictions[category_only]["category"]["status"] == "resolved"

    mutated = deepcopy(predictions)
    case_index = _case_index(
        packet,
        split="sealed",
        cohort="processor_prefix",
    )
    mutated[case_index]["category"]["target_id"] = "synthetic-category:wrong"
    report = evaluate_predictions(packet, mutated)
    assert report["ok"] is False
    assert report["metrics"]["merchant"]["precision"] == 1.0
    assert report["metrics"]["merchant"]["false_positive"] == 0
    assert report["metrics"]["category"]["false_positive"] == 1
    assert report["metrics"]["category"]["correct_coverage"] < 1.0


def test_hidden_gold_mutation_cannot_change_scorer_output():
    packet = load_packet(PACKET_DIR)
    before = _controlled(packet)
    mutated_gold = deepcopy(packet.gold_rows)
    assign_index = next(
        index
        for index, row in enumerate(mutated_gold)
        if row["merchant"]["decision"] == "assign"
    )
    mutated_gold[assign_index]["merchant"][
        "target_id"
    ] = "synthetic-merchant:mutated-hidden-gold"
    mutated_packet = replace(packet, gold_rows=tuple(mutated_gold))

    after = _controlled(mutated_packet)
    assert after == before

    report = evaluate_predictions(mutated_packet, after)
    assert report["ok"] is False
    assert report["metrics"]["merchant"]["false_positive"] == 1
    assert report["metrics"]["category"]["false_positive"] == 0


def test_physical_gold_mutation_cannot_change_restricted_scorer_output(
    tmp_path: Path,
):
    packet_dir = _copy_packet(tmp_path)
    before_packet = load_packet(packet_dir)
    before = _controlled(before_packet)
    gold_path = packet_dir / "gold.v1.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["outcomes"][0]["merchant"][
        "target_id"
    ] = "synthetic-merchant:mutated-on-disk"
    gold_path.write_bytes(canonical_json_bytes(gold))
    _refresh_manifest(packet_dir, gold_path.name)
    after_packet = load_packet(packet_dir)
    after = _controlled(after_packet)

    assert [
        (row["case_id"], row["merchant"], row["category"])
        for row in after
    ] == [
        (row["case_id"], row["merchant"], row["category"])
        for row in before
    ]
    report = evaluate_predictions(after_packet, after)
    assert report["ok"] is False
    assert report["metrics"]["merchant"]["false_positive"] == 100


def test_controlled_scorer_boundary_has_no_packet_label_or_file_access(
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = Path(controlled_scorer_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not (
        {
            "DescriptorPacket",
            "GOLD_SCHEMA_VERSION",
            "load_packet",
            "prediction_for",
        }
        & imported_names
    )
    assert "fn149_corpus" not in source
    assert "prediction_for" not in source
    forbidden_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {"read_bytes", "read_text", "write_bytes", "write_text"}
    }
    assert forbidden_calls == set()

    packet = load_packet(PACKET_DIR)
    inputs = scorer_inputs(packet)
    knowledge = deepcopy(packet.knowledge)

    def deny_file_access(*_args, **_kwargs):
        raise AssertionError("restricted scorer attempted filesystem access")

    monkeypatch.setattr(builtins, "open", deny_file_access)
    monkeypatch.setattr(Path, "read_bytes", deny_file_access)
    monkeypatch.setattr(Path, "read_text", deny_file_access)
    monkeypatch.setattr(Path, "write_bytes", deny_file_access)
    monkeypatch.setattr(Path, "write_text", deny_file_access)
    predictions = controlled_reference_predictions(
        inputs,
        knowledge,
        corpus_digest=packet.corpus_digest,
        knowledge_digest=packet.knowledge_digest,
        manifest_digest=packet.manifest_digest,
        policy_digest=packet.policy_digest,
    )
    assert len(predictions) == len(inputs)


@pytest.mark.parametrize(
    "source",
    ["local_model", "web_search", "legacy_unverified"],
)
def test_untrusted_evidence_cannot_satisfy_automatic_authority(source: str):
    packet = load_packet(PACKET_DIR)
    predictions = deepcopy(_controlled(packet))
    knowledge = deepcopy(packet.knowledge)
    knowledge_claim = next(
        claim
        for claim in knowledge["claims"]
        if claim["claim_id"] == "synthetic-claim:sealed:copper-meadow"
    )
    knowledge_claim["merchant"]["source"] = source
    knowledge_digest = hashlib.sha256(canonical_json_bytes(knowledge)).hexdigest()
    mutated_packet = replace(
        packet,
        knowledge=knowledge,
        knowledge_digest=knowledge_digest,
    )
    for prediction in predictions:
        prediction["knowledge_digest"] = knowledge_digest
        claim = prediction["merchant"]
        if (
            "synthetic-claim:sealed:copper-meadow"
            in claim["claim_ids"]
        ):
            claim["evidence_sources"] = [source]

    report = evaluate_predictions(mutated_packet, predictions)
    assert report["ok"] is False
    assert report["metrics"]["merchant"]["precision"] == 1.0
    assert report["metrics"]["merchant"]["false_positive"] == 0
    assert any(
        item.endswith(":merchant:non_authoritative_evidence")
        for item in report["authority_violations"]
    )
    assert (
        "non_authoritative_evidence_present"
        in report["automation_receipt"]["blockers"]
    )
    assert report["automation_receipt"]["enabled"] is False


def test_nonmatching_confirmed_claim_cannot_satisfy_automatic_authority():
    packet = load_packet(PACKET_DIR)
    predictions = deepcopy(_controlled(packet))
    knowledge = deepcopy(packet.knowledge)
    knowledge["claims"].append(
        {
            "claim_id": "synthetic-claim:sealed:nonmatching-copper",
            "match_text": "SYNTH SEA THIS-DOES-NOT-MATCH",
            "scope": {
                "account_id": "synthetic-account:sealed-card",
                "region": "CA-ON",
                "currency": "CAD",
            },
            "merchant": {
                "target_id": "synthetic-merchant:sealed:copper-meadow",
                "target_name": "Synthetic Copper Meadow",
                "source": "confirmed_local",
            },
        }
    )
    knowledge_digest = hashlib.sha256(canonical_json_bytes(knowledge)).hexdigest()
    mutated_packet = replace(
        packet,
        knowledge=knowledge,
        knowledge_digest=knowledge_digest,
    )
    for prediction in predictions:
        prediction["knowledge_digest"] = knowledge_digest
    case_index = _case_index(
        packet,
        split="sealed",
        cohort="processor_prefix",
    )
    predictions[case_index]["merchant"][
        "claim_ids"
    ] = ["synthetic-claim:sealed:nonmatching-copper"]

    report = evaluate_predictions(mutated_packet, predictions)
    assert report["ok"] is False
    assert any(
        item.endswith(
            "merchant claim_ids do not equal the complete "
            "descriptor-applicable set"
        )
        for item in report["violations"]
    )
    assert report["automation_receipt"]["enabled"] is False


def test_evaluator_rejects_scorer_omitting_an_applicable_conflict(
    monkeypatch: pytest.MonkeyPatch,
):
    evaluator_source_path = Path(descriptor_evaluator_module.__file__)
    evaluator_tree = ast.parse(
        evaluator_source_path.read_text(encoding="utf-8"),
        filename=str(evaluator_source_path),
    )
    scorer_helper_imports = {
        alias.name
        for node in ast.walk(evaluator_tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("fn149b_controlled_scorer")
        for alias in node.names
    }
    assert "applicable_claims" not in scorer_helper_imports

    packet = load_packet(PACKET_DIR)
    case_index = _case_index(
        packet,
        split="sealed",
        cohort="ambiguous_alias",
    )
    case_id = packet.public_cases[case_index]["case_id"]
    original_applicable_claims = controlled_scorer_module.applicable_claims

    def omit_one_applicable_conflict(
        scorer_input,
        knowledge_claims,
        claim_name,
    ):
        candidates = original_applicable_claims(
            scorer_input,
            knowledge_claims,
            claim_name,
        )
        if scorer_input["case_id"] == case_id and claim_name == "merchant":
            assert len(candidates) == 2
            return candidates[:1]
        return candidates

    monkeypatch.setattr(
        controlled_scorer_module,
        "applicable_claims",
        omit_one_applicable_conflict,
    )
    predictions = _controlled(packet)
    mutated_claim = predictions[case_index]["merchant"]
    assert mutated_claim["status"] == "resolved"
    assert mutated_claim["automatic_assignment_allowed"] is True
    assert len(mutated_claim["claim_ids"]) == 1

    report = evaluate_predictions(packet, predictions)

    assert report["ok"] is False
    assert report["metrics"]["merchant"]["false_positive"] == 1
    assert any(
        item.endswith(
            "merchant claim_ids do not equal the complete "
            "descriptor-applicable set"
        )
        for item in report["violations"]
    )
    assert report["automation_receipt"]["enabled"] is False


@pytest.mark.parametrize(
    ("cohort", "expected_status"),
    [
        ("ambiguous_alias", "abstained"),
        ("no_evidence", "no_evidence"),
        ("scope_conflict", "no_evidence"),
        ("model_hint_only", "abstained"),
        ("web_hint_only", "abstained"),
        ("legacy_alias_only", "abstained"),
    ],
)
def test_negative_cohorts_abstain_fail_closed(
    cohort: str,
    expected_status: str,
):
    packet = load_packet(PACKET_DIR)
    predictions = _controlled(packet)
    case_index = _case_index(packet, split="sealed", cohort=cohort)
    for claim_name in ("merchant", "category"):
        claim = predictions[case_index][claim_name]
        assert claim["status"] == expected_status
        assert claim["automatic_assignment_allowed"] is False
        assert claim["target_id"] is None


def test_ambiguous_automatic_assignment_breaks_cohort_abstention():
    packet = load_packet(PACKET_DIR)
    predictions = deepcopy(_controlled(packet))
    case_index = _case_index(
        packet,
        split="sealed",
        cohort="ambiguous_alias",
    )
    claim = predictions[case_index]["merchant"]
    claim.update(
        {
            "status": "resolved",
            "target_id": "synthetic-merchant:sealed:common-court-a",
            "target_name": "Synthetic Common Court A",
            "claim_ids": ["synthetic-claim:sealed:common-court-a"],
            "trust_state": "confirmed_local",
            "automatic_assignment_allowed": True,
            "evidence_sources": ["confirmed_local"],
        }
    )

    report = evaluate_predictions(packet, predictions)
    assert report["ok"] is False
    assert report["metrics"]["merchant"]["false_positive"] == 1
    assert (
        report["abstention_by_cohort"]["merchant"]["ambiguous_alias"]["rate"]
        < 1.0
    )
    assert any(
        "merchant:ambiguous_alias: abstention rate below policy" == item
        for item in report["violations"]
    )
    assert any(
        item.endswith(
            "merchant claim_ids do not equal the complete "
            "descriptor-applicable set"
        )
        for item in report["violations"]
    )


def test_controlled_reference_cannot_be_approved_as_production():
    packet = load_packet(PACKET_DIR)
    predictions = _controlled(packet)
    policy = deepcopy(packet.policy)
    policy["approved_production_tuple"] = {
        "prediction_schema_version": predictions[0]["schema_version"],
        "corpus_version": predictions[0]["corpus_version"],
        "corpus_digest": packet.corpus_digest,
        "policy_version": predictions[0]["policy_version"],
        "knowledge_version": predictions[0]["knowledge_version"],
        "knowledge_digest": packet.knowledge_digest,
        "scorer_version": CONTROLLED_SCORER_VERSION,
        "scorer_artifact_digest": CONTROLLED_SCORER_ARTIFACT_DIGEST,
        "policy_digest": packet.policy_digest,
    }
    mutated_packet = replace(packet, policy=policy)

    report = evaluate_predictions(mutated_packet, predictions)
    assert report["ok"] is True
    assert report["automation_receipt"]["enabled"] is False
    assert (
        "controlled_reference_version_has_no_authority"
        in report["automation_receipt"]["blockers"]
    )
    assert (
        "scorer_kind_is_not_production"
        in report["automation_receipt"]["blockers"]
    )


def test_unapproved_production_candidate_remains_disabled():
    packet = load_packet(PACKET_DIR)
    predictions = deepcopy(_controlled(packet))
    for prediction in predictions:
        prediction["scorer"]["kind"] = "production"
        prediction["scorer"]["version"] = "fn149b-production-candidate.v1"
        prediction["scorer"]["artifact_digest"] = "1" * 64
        prediction["scorer"]["run_id"] = "production-run:synthetic-candidate"

    report = evaluate_predictions(packet, predictions)
    assert report["ok"] is True
    assert report["evidence_class"] == "production_scoring"
    assert report["automation_receipt"]["enabled"] is False
    assert (
        "approved_production_tuple_missing"
        in report["automation_receipt"]["blockers"]
    )


def test_relabelled_controlled_output_cannot_enable_production_receipt():
    packet = load_packet(PACKET_DIR)
    predictions = deepcopy(_controlled(packet))
    scorer_version = "fn149b-production-spoof.v1"
    scorer_artifact = "1" * 64
    for prediction in predictions:
        prediction["scorer"] = {
            "kind": "production",
            "version": scorer_version,
            "artifact_digest": scorer_artifact,
            "run_id": "production-run:relabelled-controlled-output",
            "hidden_gold_access": False,
        }
    policy = deepcopy(packet.policy)
    policy["approved_production_tuple"] = {
        "prediction_schema_version": predictions[0]["schema_version"],
        "corpus_version": packet.identity["corpus_version"],
        "corpus_digest": packet.corpus_digest,
        "policy_version": packet.identity["policy_version"],
        "policy_digest": packet.policy_digest,
        "knowledge_version": packet.identity["knowledge_version"],
        "knowledge_digest": packet.knowledge_digest,
        "scorer_version": scorer_version,
        "scorer_artifact_digest": scorer_artifact,
    }

    report = evaluate_predictions(replace(packet, policy=policy), predictions)
    assert report["ok"] is True
    assert report["automation_receipt"]["enabled"] is False
    assert report["automation_receipt"]["authority"] == "disabled_fail_closed"
    assert (
        "independently_verifiable_production_run_receipt_missing"
        in report["automation_receipt"]["blockers"]
    )
    assert any(
        "independent_cluster_count_below_policy" in item
        for item in report["automation_receipt"]["blockers"]
    )


def test_hidden_gold_access_attestation_fails_evaluation_and_receipt():
    packet = load_packet(PACKET_DIR)
    predictions = deepcopy(_controlled(packet))
    predictions[0]["scorer"]["hidden_gold_access"] = True

    report = evaluate_predictions(packet, predictions)
    assert report["ok"] is False
    assert any(
        "hidden-gold non-access" in item for item in report["violations"]
    )
    assert (
        "hidden_gold_non_access_attestation_missing"
        in report["automation_receipt"]["blockers"]
    )


def test_checksum_drift_is_rejected_before_evaluation(tmp_path: Path):
    packet_dir = _copy_packet(tmp_path)
    gold_path = packet_dir / "gold.v1.json"
    gold_path.write_bytes(gold_path.read_bytes() + b" ")

    with pytest.raises(PacketError, match="gold.v1.json: checksum drift"):
        load_packet(packet_dir)


def test_scorer_visible_gold_is_rejected_even_with_refreshed_checksum(
    tmp_path: Path,
):
    packet_dir = _copy_packet(tmp_path)
    public_path = packet_dir / "public.v1.json"
    public = json.loads(public_path.read_text(encoding="utf-8"))
    public["templates"][0]["gold"] = {
        "merchant": "synthetic-merchant:should-never-be-visible"
    }
    public_path.write_bytes(canonical_json_bytes(public))
    _refresh_manifest(packet_dir, public_path.name)

    with pytest.raises(PacketError, match="forbidden scorer-visible field"):
        load_packet(packet_dir)


def test_public_input_digest_and_scope_are_bound_per_case():
    packet = load_packet(PACKET_DIR)
    predictions = deepcopy(_controlled(packet))
    predictions[0]["public_input_digest"] = "0" * 64
    predictions[1]["merchant"]["scope_fingerprint"] = "f" * 64

    report = evaluate_predictions(packet, predictions)
    assert report["ok"] is False
    assert any(
        item.endswith("public scorer input digest mismatch")
        for item in report["violations"]
    )
    assert any(
        item.endswith("merchant scope fingerprint mismatch")
        for item in report["violations"]
    )


def test_sanitizer_rebuilds_allowlisted_scope_instead_of_sharing_gold():
    packet = load_packet(PACKET_DIR)
    public_case = deepcopy(packet.public_cases[0])
    sanitized = sanitize_scorer_input(public_case)
    public_case["scope"]["account_id"] = "synthetic-account:mutated-after-copy"

    assert sanitized["scope"]["account_id"] != public_case["scope"]["account_id"]
    assert set(sanitized) == SCORER_INPUT_KEYS
