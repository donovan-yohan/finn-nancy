"""Load and expand the sealed, synthetic FN-149B descriptor corpus.

The checked-in packet keeps public descriptor templates, hidden gold labels,
and controlled-reference knowledge in separate checksum-bound files.  A scorer
receives only :func:`sanitize_scorer_input` output; the hidden labels are
materialized independently for the evaluator.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_VERSION = "fn149b-descriptor-manifest.v1"
TEMPLATE_SCHEMA_VERSION = "fn149b-descriptor-template.v1"
INPUT_SCHEMA_VERSION = "fn149b-descriptor-input.v1"
GOLD_SCHEMA_VERSION = "fn149b-descriptor-gold.v1"
PREDICTION_SCHEMA_VERSION = "fn149b-descriptor-prediction.v1"
CORPUS_VERSION = "fn149b-synthetic-descriptors.v1"
KNOWLEDGE_VERSION = "fn149b-controlled-knowledge.v1"
POLICY_VERSION = "fn149b-precision-policy.v1"
CONTROLLED_SCORER_VERSION = "fn149b-controlled-reference.v1"

REQUIRED_PACKET_FILES = {
    "public.v1.json": "public",
    "gold.v1.json": "gold",
    "knowledge.v1.json": "knowledge",
    "policy.v1.json": "policy",
}
REQUIRED_SPLITS = ("developer", "sealed")
REQUIRED_COHORT_COUNTS_PER_SPLIT = {
    "processor_prefix": 100,
    "terminal_location_codes": 100,
    "marketplace_truncation": 100,
    "legitimate_merchant_digits": 100,
    "punctuation_variants": 100,
    "subscription_suffix": 100,
    "merchant_only_confirmed": 30,
    "category_only_confirmed": 30,
    "ambiguous_alias": 20,
    "no_evidence": 20,
    "scope_conflict": 20,
    "model_hint_only": 20,
    "web_hint_only": 20,
    "legacy_alias_only": 20,
}
SCORER_INPUT_KEYS = frozenset(
    {
        "schema_version",
        "corpus_version",
        "case_id",
        "split",
        "descriptor",
        "scope",
    }
)
FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "gold",
        "expected",
        "expectation",
        "label",
        "labels",
        "merchant_id",
        "category_id",
        "target_id",
        "target_name",
    }
)


class PacketError(ValueError):
    """Raised when the sealed descriptor packet is malformed or has drifted."""


@dataclass(frozen=True)
class DescriptorPacket:
    """Expanded packet with scorer-visible and evaluator-only data separated."""

    manifest: dict[str, Any]
    policy: dict[str, Any]
    knowledge: dict[str, Any]
    public_cases: tuple[dict[str, Any], ...]
    gold_rows: tuple[dict[str, Any], ...]
    manifest_digest: str
    corpus_digest: str
    knowledge_digest: str
    policy_digest: str

    @property
    def identity(self) -> dict[str, str]:
        return {
            "manifest_version": MANIFEST_VERSION,
            "manifest_digest": self.manifest_digest,
            "corpus_version": CORPUS_VERSION,
            "corpus_digest": self.corpus_digest,
            "knowledge_version": KNOWLEDGE_VERSION,
            "knowledge_digest": self.knowledge_digest,
            "policy_version": POLICY_VERSION,
            "policy_digest": self.policy_digest,
        }


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical representation used by all packet digests."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def canonical_jsonl_bytes(rows: tuple[dict[str, Any], ...]) -> bytes:
    return b"".join(canonical_json_bytes(row) for row in rows)


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def policy_contract_digest(policy: dict[str, Any]) -> str:
    """Digest policy gates without the self-referential approval receipt."""

    contract = dict(policy)
    contract["approved_production_tuple"] = None
    return sha256_hex(canonical_json_bytes(contract))


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PacketError(f"{path.name}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PacketError(f"{path.name}: root must be an object")
    return value


def _safe_child(root: Path, relative: str) -> Path:
    child = Path(relative)
    if child.is_absolute() or ".." in child.parts or len(child.parts) != 1:
        raise PacketError(f"unsafe packet path {relative!r}")
    return root / child


def _assert_no_forbidden_public_keys(value: Any, path: str = "public") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in FORBIDDEN_PUBLIC_KEYS:
                raise PacketError(f"{path}: forbidden scorer-visible field {key!r}")
            _assert_no_forbidden_public_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_forbidden_public_keys(child, f"{path}[{index}]")


def _scope(value: Any, *, context: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PacketError(f"{context}: scope must be an object")
    required = {"account_id", "region", "currency"}
    if set(value) != required:
        raise PacketError(f"{context}: scope fields must equal {sorted(required)!r}")
    result: dict[str, str] = {}
    for key in sorted(required):
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise PacketError(f"{context}: scope {key} must be a non-empty string")
        if key == "account_id" and not item.startswith("synthetic-account:"):
            raise PacketError(f"{context}: account scope is not synthetic")
        result[key] = item
    return result


def scope_fingerprint(scope: dict[str, str]) -> str:
    return sha256_hex(canonical_json_bytes(scope))


def sanitize_scorer_input(case: dict[str, Any]) -> dict[str, Any]:
    """Project a public case onto the only fields a scorer may observe."""

    missing = SCORER_INPUT_KEYS - set(case)
    if missing:
        raise PacketError(f"{case.get('case_id', '<unknown>')}: missing scorer fields")
    result = {
        "schema_version": case["schema_version"],
        "corpus_version": case["corpus_version"],
        "case_id": case["case_id"],
        "split": case["split"],
        "descriptor": case["descriptor"],
        "scope": dict(case["scope"]),
    }
    if set(result) != SCORER_INPUT_KEYS:
        raise AssertionError("scorer projection drifted")
    return result


def scorer_inputs(packet: DescriptorPacket) -> tuple[dict[str, Any], ...]:
    return tuple(sanitize_scorer_input(case) for case in packet.public_cases)


def _load_documents(
    root: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    bytes,
]:
    manifest_path = root / "manifest.v1.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise PacketError(f"manifest.v1.json: cannot read: {exc}") from exc
    manifest = _read_object(manifest_path)
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise PacketError("manifest version is not approved")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise PacketError("manifest files must be a list")
    seen: dict[str, str] = {}
    documents: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PacketError("manifest file entry must be an object")
        relative = entry.get("path")
        kind = entry.get("kind")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(kind, str):
            raise PacketError("manifest file entry is missing path or kind")
        if relative in seen:
            raise PacketError(f"manifest repeats {relative}")
        if REQUIRED_PACKET_FILES.get(relative) != kind:
            raise PacketError(f"{relative}: unexpected manifest kind {kind!r}")
        path = _safe_child(root, relative)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise PacketError(f"{relative}: cannot read: {exc}") from exc
        if digest != sha256_hex(payload):
            raise PacketError(f"{relative}: checksum drift")
        seen[relative] = kind
        documents[kind] = _read_object(path)
    if seen != REQUIRED_PACKET_FILES:
        raise PacketError(
            f"manifest files must equal {sorted(REQUIRED_PACKET_FILES)!r}"
        )
    return (
        manifest,
        documents["public"],
        documents["gold"],
        documents["knowledge"],
        documents["policy"],
        manifest_bytes,
    )


def _expand_public(public: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if public.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
        raise PacketError("public template schema version is not approved")
    if public.get("corpus_version") != CORPUS_VERSION:
        raise PacketError("public corpus version is not approved")
    privacy = public.get("privacy")
    if privacy != {
        "contains_personal_data": False,
        "production_data_forbidden": True,
        "source": "invented_synthetic_templates",
    }:
        raise PacketError("public privacy declaration is invalid")
    _assert_no_forbidden_public_keys(public)
    templates = public.get("templates")
    if not isinstance(templates, list) or not templates:
        raise PacketError("public templates must be a non-empty list")

    cases: list[dict[str, Any]] = []
    seen_template_ids: set[str] = set()
    seen_case_ids: set[str] = set()
    for template in templates:
        if not isinstance(template, dict):
            raise PacketError("public template must be an object")
        template_id = template.get("template_id")
        split = template.get("split")
        cohort = template.get("cohort")
        pattern = template.get("descriptor_pattern")
        case_count = template.get("case_count")
        family_size = template.get("family_size")
        if (
            not isinstance(template_id, str)
            or not template_id.startswith("synthetic-template:")
            or template_id in seen_template_ids
        ):
            raise PacketError(f"invalid or duplicate template id {template_id!r}")
        seen_template_ids.add(template_id)
        if split not in REQUIRED_SPLITS:
            raise PacketError(f"{template_id}: invalid split {split!r}")
        if not isinstance(cohort, str) or not cohort:
            raise PacketError(f"{template_id}: invalid cohort")
        if not isinstance(pattern, str) or not pattern.startswith("SYNTH "):
            raise PacketError(f"{template_id}: descriptor pattern is not synthetic")
        if not isinstance(case_count, int) or case_count <= 0:
            raise PacketError(f"{template_id}: case_count must be positive")
        if not isinstance(family_size, int) or family_size <= 0:
            raise PacketError(f"{template_id}: family_size must be positive")
        scope = _scope(template.get("scope"), context=template_id)
        for index in range(case_count):
            try:
                descriptor = pattern.format(
                    serial=index,
                    family=index // family_size,
                    terminal=(index * 17 + 11) % 997,
                    location=(index * 7 + 3) % 89,
                )
            except (KeyError, ValueError) as exc:
                raise PacketError(
                    f"{template_id}: invalid descriptor pattern: {exc}"
                ) from exc
            case_id = f"{template_id}:case:{index:04d}"
            if case_id in seen_case_ids:
                raise PacketError(f"duplicate case id {case_id}")
            seen_case_ids.add(case_id)
            cases.append(
                {
                    "schema_version": INPUT_SCHEMA_VERSION,
                    "corpus_version": CORPUS_VERSION,
                    "case_id": case_id,
                    "split": split,
                    "family_id": f"{template_id}:family:{index // family_size:04d}",
                    "template_id": template_id,
                    "cohort": cohort,
                    "descriptor": descriptor,
                    "scope": scope,
                    "privacy_tag": "deterministic_synthetic",
                }
            )

    family_by_split = {
        split: {
            str(case["family_id"])
            for case in cases
            if case["split"] == split
        }
        for split in REQUIRED_SPLITS
    }
    overlap = family_by_split["developer"] & family_by_split["sealed"]
    if overlap:
        raise PacketError(f"developer/sealed family leakage: {sorted(overlap)[:3]!r}")
    descriptors_by_split = {
        split: {
            str(case["descriptor"]).casefold()
            for case in cases
            if case["split"] == split
        }
        for split in REQUIRED_SPLITS
    }
    overlap_descriptors = (
        descriptors_by_split["developer"] & descriptors_by_split["sealed"]
    )
    if overlap_descriptors:
        raise PacketError(
            "developer/sealed descriptor leakage: "
            f"{sorted(overlap_descriptors)[:3]!r}"
        )
    actual_counts = {
        split: Counter(
            str(case["cohort"])
            for case in cases
            if case["split"] == split
        )
        for split in REQUIRED_SPLITS
    }
    for split in REQUIRED_SPLITS:
        if actual_counts[split] != Counter(REQUIRED_COHORT_COUNTS_PER_SPLIT):
            raise PacketError(
                f"{split}: descriptor cohort counts do not match the sealed contract"
            )
    return tuple(cases)


def _claim_expectation(
    value: Any, *, template_id: str, claim_name: str
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PacketError(f"{template_id}: {claim_name} gold must be an object")
    decision = value.get("decision")
    if decision == "abstain" and set(value) == {"decision"}:
        return {"decision": "abstain"}
    if decision != "assign" or set(value) != {"decision", "target_id"}:
        raise PacketError(f"{template_id}: invalid {claim_name} gold")
    target_id = value.get("target_id")
    prefix = (
        "synthetic-merchant:"
        if claim_name == "merchant"
        else "synthetic-category:"
    )
    if not isinstance(target_id, str) or not target_id.startswith(prefix):
        raise PacketError(f"{template_id}: {claim_name} target is not synthetic")
    return {"decision": "assign", "target_id": target_id}


def _expand_gold(
    gold: dict[str, Any], public_cases: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], ...]:
    if gold.get("schema_version") != GOLD_SCHEMA_VERSION:
        raise PacketError("gold schema version is not approved")
    if gold.get("corpus_version") != CORPUS_VERSION:
        raise PacketError("gold corpus version is not approved")
    outcomes = gold.get("outcomes")
    if not isinstance(outcomes, list):
        raise PacketError("gold outcomes must be a list")
    by_template: dict[str, dict[str, dict[str, str]]] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            raise PacketError("gold outcome must be an object")
        template_id = outcome.get("template_id")
        if not isinstance(template_id, str) or template_id in by_template:
            raise PacketError(f"invalid or duplicate gold template {template_id!r}")
        by_template[template_id] = {
            claim: _claim_expectation(
                outcome.get(claim),
                template_id=template_id,
                claim_name=claim,
            )
            for claim in ("merchant", "category")
        }
    public_templates = {str(case["template_id"]) for case in public_cases}
    if set(by_template) != public_templates:
        raise PacketError("gold templates do not cover public templates exactly")
    return tuple(
        {
            "schema_version": GOLD_SCHEMA_VERSION,
            "corpus_version": CORPUS_VERSION,
            "case_id": case["case_id"],
            "split": case["split"],
            "cohort": case["cohort"],
            "merchant": dict(by_template[str(case["template_id"])]["merchant"]),
            "category": dict(by_template[str(case["template_id"])]["category"]),
        }
        for case in public_cases
    )


def _validate_knowledge(knowledge: dict[str, Any]) -> None:
    if knowledge.get("knowledge_version") != KNOWLEDGE_VERSION:
        raise PacketError("knowledge version is not approved")
    privacy = knowledge.get("privacy")
    if privacy != {
        "contains_personal_data": False,
        "production_data_forbidden": True,
        "source": "invented_synthetic_claims",
    }:
        raise PacketError("knowledge privacy declaration is invalid")
    claims = knowledge.get("claims")
    if not isinstance(claims, list):
        raise PacketError("knowledge claims must be a list")
    seen: set[str] = set()
    allowed_sources = {
        "confirmed_local",
        "local_model",
        "web_search",
        "legacy_unverified",
    }
    for claim in claims:
        if not isinstance(claim, dict):
            raise PacketError("knowledge claim must be an object")
        claim_id = claim.get("claim_id")
        match_text = claim.get("match_text")
        if (
            not isinstance(claim_id, str)
            or not claim_id.startswith("synthetic-claim:")
            or claim_id in seen
        ):
            raise PacketError(f"invalid or duplicate knowledge claim {claim_id!r}")
        seen.add(claim_id)
        if not isinstance(match_text, str) or not match_text.startswith("SYNTH "):
            raise PacketError(f"{claim_id}: match text is not synthetic")
        _scope(claim.get("scope"), context=claim_id)
        for claim_name, prefix in (
            ("merchant", "synthetic-merchant:"),
            ("category", "synthetic-category:"),
        ):
            resolution = claim.get(claim_name)
            if resolution is None:
                continue
            if not isinstance(resolution, dict):
                raise PacketError(f"{claim_id}: {claim_name} must be an object")
            if set(resolution) != {"target_id", "target_name", "source"}:
                raise PacketError(f"{claim_id}: invalid {claim_name} fields")
            target_id = resolution.get("target_id")
            target_name = resolution.get("target_name")
            source = resolution.get("source")
            if not isinstance(target_id, str) or not target_id.startswith(prefix):
                raise PacketError(f"{claim_id}: {claim_name} target is not synthetic")
            if not isinstance(target_name, str) or not target_name.startswith("Synthetic "):
                raise PacketError(f"{claim_id}: {claim_name} name is not synthetic")
            if source not in allowed_sources:
                raise PacketError(f"{claim_id}: unsupported evidence source {source!r}")


def load_packet(packet_dir: str | Path) -> DescriptorPacket:
    """Verify checksums and expand the deterministic packet."""

    root = Path(packet_dir)
    (
        manifest,
        public,
        gold,
        knowledge,
        policy,
        manifest_bytes,
    ) = _load_documents(root)
    public_cases = _expand_public(public)
    gold_rows = _expand_gold(gold, public_cases)
    _validate_knowledge(knowledge)
    return DescriptorPacket(
        manifest=manifest,
        policy=policy,
        knowledge=knowledge,
        public_cases=public_cases,
        gold_rows=gold_rows,
        manifest_digest=sha256_hex(manifest_bytes),
        corpus_digest=sha256_hex(
            canonical_jsonl_bytes(
                tuple(sanitize_scorer_input(case) for case in public_cases)
            )
        ),
        knowledge_digest=sha256_hex(canonical_json_bytes(knowledge)),
        policy_digest=policy_contract_digest(policy),
    )
