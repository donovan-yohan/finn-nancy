"""Append-only scoped merchant and expense-category knowledge."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from . import migrate
from ..reconcile.descriptor_normalization import (
    NORMALIZATION_VERSION,
    NormalizedDescriptor,
    normalize_descriptor_v2,
)

MAPPING_SCHEMA_VERSION = "fn149b-merchant-knowledge.v1"
ClaimKind = Literal["canonical_merchant", "expense_category"]
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9:._/-]{0,240}$")


@dataclass(frozen=True)
class MerchantScope:
    household_scope: str
    account_id: int | None = None
    provider_identity_hash: str = ""
    processor_family: str = ""
    region: str = ""

    @property
    def fingerprint(self) -> str:
        payload = {
            "account_id": self.account_id if self.account_id is not None else 0,
            "household_scope": self.household_scope,
            "processor_family": self.processor_family,
            "provider_identity_hash": self.provider_identity_hash,
            "region": self.region,
        }
        return _digest(payload)

    def dimensions(self) -> tuple[object, ...]:
        return (
            self.account_id,
            self.provider_identity_hash,
            self.processor_family,
            self.region,
        )


@dataclass(frozen=True)
class Evidence:
    statement_line_id: int | None = None
    transaction_id: int | None = None
    transaction_split_id: int | None = None
    source_anchor_id: int | None = None
    proposed_action_id: int | None = None


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _opaque(value: str, label: str, *, required: bool = False) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ValueError(f"{label} is required")
    if not _OPAQUE_REF.fullmatch(normalized):
        raise ValueError(f"{label} must be an opaque identifier")
    return normalized


def _citation(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "citation URL must be an http(s) URL without credentials, query, or fragment"
        )
    if len(normalized) > 1000:
        raise ValueError("citation URL is too long")
    return normalized


def scope_for(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    provider_identity_hash: str = "",
    processor_family: str = "",
    region: str = "",
) -> MerchantScope:
    household_scope = migrate.read_database_identity(conn)
    if household_scope is None:
        raise RuntimeError("database identity is unavailable")
    provider = str(provider_identity_hash or "").strip().lower()
    if provider and (len(provider) != 64 or any(c not in "0123456789abcdef" for c in provider)):
        raise ValueError("provider identity hash must be 64 lowercase hex characters")
    if provider and account_id is None:
        raise ValueError("provider identity scope requires an account")
    processor = str(processor_family or "").strip().casefold()
    location = str(region or "").strip().upper()
    if len(processor) > 64:
        raise ValueError("processor family is too long")
    if len(location) > 32:
        raise ValueError("region is too long")
    if account_id is not None:
        account = conn.execute(
            "SELECT 1 FROM accounts WHERE id=?",
            (int(account_id),),
        ).fetchone()
        if account is None:
            raise ValueError("merchant scope account does not exist")
    return MerchantScope(
        household_scope=household_scope,
        account_id=int(account_id) if account_id is not None else None,
        provider_identity_hash=provider,
        processor_family=processor,
        region=location,
    )


def scope_for_transaction(
    conn: sqlite3.Connection,
    transaction_id: int,
    *,
    processor_family: str = "",
    region: str = "",
) -> MerchantScope:
    row = conn.execute(
        """
        SELECT
          txn.account_id,
          COALESCE(imported_row.provider_identity_hash, '') AS provider_identity_hash
        FROM transactions txn
        LEFT JOIN statement_lines line
          ON line.matched_transaction_id = txn.id
         AND line.review_disposition = 'active'
        LEFT JOIN structured_statement_import_rows imported_row
          ON imported_row.statement_line_id = line.id
        WHERE txn.id=?
        ORDER BY imported_row.id DESC
        LIMIT 1
        """,
        (int(transaction_id),),
    ).fetchone()
    if row is None:
        raise ValueError("transaction does not exist")
    return scope_for(
        conn,
        account_id=int(row["account_id"]),
        provider_identity_hash=str(row["provider_identity_hash"] or ""),
        processor_family=processor_family,
        region=region,
    )


def scope_for_statement_line(
    conn: sqlite3.Connection,
    statement_line_id: int,
    *,
    processor_family: str = "",
    region: str = "",
) -> MerchantScope:
    row = conn.execute(
        """
        SELECT
          line.account_id,
          COALESCE(imported_row.provider_identity_hash, '') AS provider_identity_hash
        FROM statement_lines line
        LEFT JOIN structured_statement_import_rows imported_row
          ON imported_row.statement_line_id = line.id
        WHERE line.id=? AND line.review_disposition='active'
        ORDER BY imported_row.id DESC
        LIMIT 1
        """,
        (int(statement_line_id),),
    ).fetchone()
    if row is None:
        raise ValueError("statement line does not exist")
    return scope_for(
        conn,
        account_id=int(row["account_id"]) if row["account_id"] is not None else None,
        provider_identity_hash=str(row["provider_identity_hash"] or ""),
        processor_family=processor_family,
        region=region,
    )


def _ensure_pattern(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    scope: MerchantScope,
    actor: str,
) -> tuple[int, NormalizedDescriptor]:
    normalized = normalize_descriptor_v2(descriptor)
    pattern_key = "pattern:" + _digest(
        {
            "fingerprint": normalized.fingerprint,
            "scope_fingerprint": scope.fingerprint,
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO merchant_descriptor_patterns(
          pattern_key, normalization_version, pattern_kind, pattern_json,
          pattern_fingerprint, household_scope, account_id,
          provider_identity_hash, processor_family, region,
          scope_fingerprint, created_by
        )
        VALUES (?, ?, 'exact_tokens', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pattern_key,
            NORMALIZATION_VERSION,
            normalized.pattern_json,
            normalized.fingerprint,
            scope.household_scope,
            scope.account_id,
            scope.provider_identity_hash,
            scope.processor_family,
            scope.region,
            scope.fingerprint,
            _opaque(actor, "actor", required=True),
        ),
    )
    row = conn.execute(
        """
        SELECT id, pattern_json, household_scope, account_id,
               provider_identity_hash, processor_family, region
        FROM merchant_descriptor_patterns
        WHERE pattern_key=?
        """,
        (pattern_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to create merchant descriptor pattern")
    if (
        row["pattern_json"] != normalized.pattern_json
        or row["household_scope"] != scope.household_scope
        or row["account_id"] != scope.account_id
        or row["provider_identity_hash"] != scope.provider_identity_hash
        or row["processor_family"] != scope.processor_family
        or row["region"] != scope.region
    ):
        raise RuntimeError("merchant descriptor pattern key collision")
    return int(row["id"]), normalized


def _ensure_entity(
    conn: sqlite3.Connection,
    *,
    canonical_name: str,
    household_scope: str,
    actor: str,
) -> int:
    display = str(canonical_name or "").strip()
    if not display or len(display) > 160:
        raise ValueError("canonical merchant name must contain 1-160 characters")
    normalized = normalize_descriptor_v2(display).value
    entity_key = "merchant:" + _digest(
        {"household_scope": household_scope, "normalized_name": normalized}
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO merchant_entities(
          entity_key, canonical_name, normalized_name, created_by
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            entity_key,
            display,
            normalized,
            _opaque(actor, "actor", required=True),
        ),
    )
    row = conn.execute(
        """
        SELECT id, canonical_name, normalized_name
        FROM merchant_entities
        WHERE entity_key=?
        """,
        (entity_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to create merchant entity")
    if row["normalized_name"] != normalized:
        raise RuntimeError("merchant entity key collision")
    return int(row["id"])


def _claim_target(
    conn: sqlite3.Connection,
    *,
    claim_kind: ClaimKind,
    canonical_name: str = "",
    category_id: int | None = None,
    household_scope: str,
    actor: str,
) -> tuple[int | None, int | None, str]:
    if claim_kind == "canonical_merchant":
        entity_id = _ensure_entity(
            conn,
            canonical_name=canonical_name,
            household_scope=household_scope,
            actor=actor,
        )
        return entity_id, None, f"merchant:{entity_id}"
    if category_id is None:
        raise ValueError("expense category claim requires a category")
    category = conn.execute(
        """
        SELECT id, name
        FROM categories
        WHERE id=? AND kind='expense' AND name <> 'Uncategorized'
        """,
        (int(category_id),),
    ).fetchone()
    if category is None:
        raise ValueError("expense category claim target is invalid")
    return None, int(category["id"]), f"category:{int(category['id'])}"


def _existing_event(
    conn: sqlite3.Connection, operation_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM merchant_resolution_events WHERE operation_key=?",
        (operation_key,),
    ).fetchone()


def _insert_event(
    conn: sqlite3.Connection,
    *,
    operation_key: str,
    claim_id: int,
    event_kind: str,
    trust_state: str,
    actor_kind: str,
    actor: str,
    reason: str,
    provenance_kind: str,
    provenance_ref: str = "",
    evidence: Evidence = Evidence(),
    citation_url: str = "",
    consent_version: str = "",
    reverses_event_id: int | None = None,
) -> int:
    operation = _opaque(operation_key, "operation key", required=True)
    existing = _existing_event(conn, operation)
    if existing is not None:
        if int(existing["claim_id"]) != int(claim_id) or existing["event_kind"] != event_kind:
            raise ValueError("merchant event operation key was reused")
        return int(existing["id"])
    actor_value = _opaque(actor, "actor", required=True)
    provenance_ref_value = _opaque(provenance_ref, "provenance reference")
    consent = _opaque(consent_version, "consent version")
    citation = _citation(citation_url)
    reason_value = str(reason or "").strip()
    if not reason_value or len(reason_value) > 500:
        raise ValueError("merchant resolution reason must contain 1-500 characters")
    cur = conn.execute(
        """
        INSERT INTO merchant_resolution_events(
          operation_key, claim_id, event_kind, trust_state,
          actor_kind, actor, reason, provenance_kind, provenance_ref,
          statement_line_id, transaction_id, transaction_split_id,
          source_anchor_id, proposed_action_id, citation_url, consent_version,
          reverses_event_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation,
            int(claim_id),
            event_kind,
            trust_state,
            actor_kind,
            actor_value,
            reason_value,
            provenance_kind,
            provenance_ref_value,
            evidence.statement_line_id,
            evidence.transaction_id,
            evidence.transaction_split_id,
            evidence.source_anchor_id,
            evidence.proposed_action_id,
            citation,
            consent,
            reverses_event_id,
        ),
    )
    return int(cur.lastrowid)


def _create_claim_with_event(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    scope: MerchantScope,
    claim_kind: ClaimKind,
    operation_key: str,
    actor_kind: str,
    actor: str,
    reason: str,
    provenance_kind: str,
    event_kind: str,
    trust_state: str,
    canonical_name: str = "",
    category_id: int | None = None,
    evidence: Evidence = Evidence(),
    provenance_ref: str = "",
    citation_url: str = "",
    consent_version: str = "",
    supersedes_claim_id: int | None = None,
) -> int:
    operation = _opaque(operation_key, "operation key", required=True)
    existing = _existing_event(conn, operation)
    if existing is not None:
        normalized_descriptor = normalize_descriptor_v2(descriptor)
        row = conn.execute(
            """
            SELECT
              claim.claim_kind,
              claim.category_id,
              pattern.pattern_fingerprint,
              pattern.scope_fingerprint,
              entity.normalized_name
            FROM merchant_resolution_claims claim
            JOIN merchant_descriptor_patterns pattern
              ON pattern.id=claim.pattern_id
            LEFT JOIN merchant_entities entity
              ON entity.id=claim.merchant_entity_id
            WHERE claim.id=?
            """,
            (int(existing["claim_id"]),),
        ).fetchone()
        expected_merchant_name = (
            normalize_descriptor_v2(canonical_name).value
            if claim_kind == "canonical_merchant"
            else None
        )
        if (
            row is None
            or row["claim_kind"] != claim_kind
            or row["pattern_fingerprint"]
            != normalized_descriptor.fingerprint
            or row["scope_fingerprint"] != scope.fingerprint
            or (
                claim_kind == "canonical_merchant"
                and row["normalized_name"] != expected_merchant_name
            )
            or (
                claim_kind == "expense_category"
                and int(row["category_id"] or 0) != int(category_id or 0)
            )
        ):
            raise ValueError("merchant event operation key was reused")
        return int(existing["claim_id"])
    pattern_id, _ = _ensure_pattern(
        conn,
        descriptor=descriptor,
        scope=scope,
        actor=actor,
    )
    merchant_entity_id, target_category_id, target_key = _claim_target(
        conn,
        claim_kind=claim_kind,
        canonical_name=canonical_name,
        category_id=category_id,
        household_scope=scope.household_scope,
        actor=actor,
    )
    claim_key = "claim:" + _digest(
        {
            "claim_kind": claim_kind,
            "operation_key": operation,
            "pattern_id": pattern_id,
            "target": target_key,
        }
    )
    cur = conn.execute(
        """
        INSERT INTO merchant_resolution_claims(
          claim_key, pattern_id, claim_kind, merchant_entity_id, category_id,
          supersedes_claim_id, created_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_key,
            pattern_id,
            claim_kind,
            merchant_entity_id,
            target_category_id,
            supersedes_claim_id,
            _opaque(actor, "actor", required=True),
        ),
    )
    claim_id = int(cur.lastrowid)
    _insert_event(
        conn,
        operation_key=operation,
        claim_id=claim_id,
        event_kind=event_kind,
        trust_state=trust_state,
        actor_kind=actor_kind,
        actor=actor,
        reason=reason,
        provenance_kind=provenance_kind,
        provenance_ref=provenance_ref,
        evidence=evidence,
        citation_url=citation_url,
        consent_version=consent_version,
    )
    return claim_id


def _validate_human_evidence(
    evidence: Evidence,
    *,
    claim_kind: ClaimKind,
) -> None:
    if (
        evidence.statement_line_id is None
        and evidence.transaction_id is None
        and evidence.proposed_action_id is None
    ):
        raise ValueError("human confirmation requires a durable decision subject")
    if claim_kind == "expense_category" and (
        evidence.transaction_id is None
        or evidence.transaction_split_id is None
    ):
        raise ValueError(
            "category confirmation requires transaction and split evidence"
        )


def confirm_merchant(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    canonical_name: str,
    scope: MerchantScope,
    operation_key: str,
    actor: str,
    reason: str,
    evidence: Evidence,
    provenance_kind: str = "operator",
    provenance_ref: str = "",
    citation_url: str = "",
    consent_version: str = "",
) -> int:
    _validate_human_evidence(evidence, claim_kind="canonical_merchant")
    return _create_claim_with_event(
        conn,
        descriptor=descriptor,
        scope=scope,
        claim_kind="canonical_merchant",
        canonical_name=canonical_name,
        operation_key=operation_key,
        actor_kind="human",
        actor=actor,
        reason=reason,
        provenance_kind=provenance_kind,
        provenance_ref=provenance_ref,
        event_kind="accepted",
        trust_state="human_confirmed",
        evidence=evidence,
        citation_url=citation_url,
        consent_version=consent_version,
    )


def confirm_category(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    category_id: int,
    scope: MerchantScope,
    operation_key: str,
    actor: str,
    reason: str,
    evidence: Evidence,
    provenance_kind: str = "manual_recategorization",
    provenance_ref: str = "",
    citation_url: str = "",
    consent_version: str = "",
) -> int:
    _validate_human_evidence(evidence, claim_kind="expense_category")
    return _create_claim_with_event(
        conn,
        descriptor=descriptor,
        scope=scope,
        claim_kind="expense_category",
        category_id=category_id,
        operation_key=operation_key,
        actor_kind="human",
        actor=actor,
        reason=reason,
        provenance_kind=provenance_kind,
        provenance_ref=provenance_ref,
        event_kind="accepted",
        trust_state="human_confirmed",
        evidence=evidence,
        citation_url=citation_url,
        consent_version=consent_version,
    )


def propose_merchant(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    canonical_name: str,
    scope: MerchantScope,
    operation_key: str,
    actor_kind: Literal["model", "web", "system"],
    actor: str,
    reason: str,
    evidence: Evidence = Evidence(),
    provenance_ref: str = "",
    citation_url: str = "",
) -> int:
    if actor_kind == "web" and not str(citation_url or "").strip():
        raise ValueError("web merchant proposal requires a citation URL")
    provenance_kind = {
        "model": "model_proposal",
        "web": "web_search",
        "system": "system",
    }[actor_kind]
    return _create_claim_with_event(
        conn,
        descriptor=descriptor,
        scope=scope,
        claim_kind="canonical_merchant",
        canonical_name=canonical_name,
        operation_key=operation_key,
        actor_kind=actor_kind,
        actor=actor,
        reason=reason,
        provenance_kind=provenance_kind,
        provenance_ref=provenance_ref,
        event_kind="proposed",
        trust_state="untrusted_proposal",
        evidence=evidence,
        citation_url=citation_url,
    )


def propose_category(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    category_id: int,
    scope: MerchantScope,
    operation_key: str,
    actor_kind: Literal["model", "web", "system"],
    actor: str,
    reason: str,
    evidence: Evidence = Evidence(),
    provenance_ref: str = "",
    citation_url: str = "",
) -> int:
    if actor_kind == "web" and not str(citation_url or "").strip():
        raise ValueError("web category proposal requires a citation URL")
    provenance_kind = {
        "model": "model_proposal",
        "web": "web_search",
        "system": "system",
    }[actor_kind]
    return _create_claim_with_event(
        conn,
        descriptor=descriptor,
        scope=scope,
        claim_kind="expense_category",
        category_id=category_id,
        operation_key=operation_key,
        actor_kind=actor_kind,
        actor=actor,
        reason=reason,
        provenance_kind=provenance_kind,
        provenance_ref=provenance_ref,
        event_kind="proposed",
        trust_state="untrusted_proposal",
        evidence=evidence,
        citation_url=citation_url,
    )


def accept_proposal(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    operation_key: str,
    actor: str,
    reason: str,
    evidence: Evidence,
    citation_url: str = "",
    consent_version: str = "",
) -> int:
    current = current_claim(conn, claim_id)
    if current is None or current["event_kind"] not in {
        "proposed",
        "legacy_imported",
    }:
        raise ValueError("merchant resolution claim is not awaiting acceptance")
    _validate_human_evidence(
        evidence,
        claim_kind=str(current["claim_kind"]),
    )
    return _insert_event(
        conn,
        operation_key=operation_key,
        claim_id=claim_id,
        event_kind="accepted",
        trust_state="human_confirmed",
        actor_kind="human",
        actor=actor,
        reason=reason,
        provenance_kind=str(current["provenance_kind"]),
        provenance_ref=str(current["provenance_ref"]),
        evidence=evidence,
        citation_url=citation_url,
        consent_version=consent_version,
    )


def reject_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    current = current_claim(conn, claim_id)
    if current is None or current["event_kind"] not in {
        "proposed",
        "legacy_imported",
    }:
        raise ValueError("merchant resolution claim is not awaiting rejection")
    return _insert_event(
        conn,
        operation_key=operation_key,
        claim_id=claim_id,
        event_kind="rejected",
        trust_state="rejected",
        actor_kind="human",
        actor=actor,
        reason=reason,
        provenance_kind=str(current["provenance_kind"]),
        provenance_ref=str(current["provenance_ref"]),
        evidence=Evidence(
            statement_line_id=current["statement_line_id"],
            transaction_id=current["transaction_id"],
            transaction_split_id=current["transaction_split_id"],
            source_anchor_id=current["source_anchor_id"],
            proposed_action_id=current["proposed_action_id"],
        ),
    )


def _reverse_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    event_kind: Literal["retired", "undo"],
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    current = current_claim(conn, claim_id)
    if current is None or current["event_kind"] not in {"accepted", "corrected"}:
        raise ValueError("merchant resolution claim is not active")
    return _insert_event(
        conn,
        operation_key=operation_key,
        claim_id=claim_id,
        event_kind=event_kind,
        trust_state="retired",
        actor_kind="human",
        actor=actor,
        reason=reason,
        provenance_kind=str(current["provenance_kind"]),
        provenance_ref=str(current["provenance_ref"]),
        evidence=Evidence(
            statement_line_id=current["statement_line_id"],
            transaction_id=current["transaction_id"],
            transaction_split_id=current["transaction_split_id"],
            source_anchor_id=current["source_anchor_id"],
            proposed_action_id=current["proposed_action_id"],
        ),
        reverses_event_id=int(current["current_event_id"]),
    )


def undo_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    return _reverse_claim(
        conn,
        claim_id=claim_id,
        event_kind="undo",
        operation_key=operation_key,
        actor=actor,
        reason=reason,
    )


def retire_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    return _reverse_claim(
        conn,
        claim_id=claim_id,
        event_kind="retired",
        operation_key=operation_key,
        actor=actor,
        reason=reason,
    )


def _correct_claim(
    conn: sqlite3.Connection,
    *,
    prior_claim_id: int,
    descriptor: str,
    scope: MerchantScope,
    claim_kind: ClaimKind,
    operation_key: str,
    actor: str,
    reason: str,
    evidence: Evidence,
    canonical_name: str = "",
    category_id: int | None = None,
    provenance_kind: str = "operator",
    provenance_ref: str = "",
) -> int:
    _validate_human_evidence(evidence, claim_kind=claim_kind)
    prior = current_claim(conn, prior_claim_id)
    if prior is None or prior["event_kind"] not in {"accepted", "corrected"}:
        existing = _existing_event(conn, operation_key)
        if existing is not None:
            return int(existing["claim_id"])
        raise ValueError("merchant correction requires an active prior claim")
    replacement_id = _create_claim_with_event(
        conn,
        descriptor=descriptor,
        scope=scope,
        claim_kind=claim_kind,
        canonical_name=canonical_name,
        category_id=category_id,
        operation_key=operation_key,
        actor_kind="human",
        actor=actor,
        reason=reason,
        provenance_kind=provenance_kind,
        provenance_ref=provenance_ref,
        event_kind="corrected",
        trust_state="human_confirmed",
        evidence=evidence,
        supersedes_claim_id=prior_claim_id,
    )
    prior_after = current_claim(conn, prior_claim_id)
    if prior_after is not None and prior_after["event_kind"] in {
        "accepted",
        "corrected",
    }:
        retire_claim(
            conn,
            claim_id=prior_claim_id,
            operation_key=f"{operation_key}:retire-prior",
            actor=actor,
            reason="prior scoped claim retired by correction",
        )
    return replacement_id


def correct_merchant(
    conn: sqlite3.Connection,
    *,
    prior_claim_id: int,
    descriptor: str,
    canonical_name: str,
    scope: MerchantScope,
    operation_key: str,
    actor: str,
    reason: str,
    evidence: Evidence,
) -> int:
    return _correct_claim(
        conn,
        prior_claim_id=prior_claim_id,
        descriptor=descriptor,
        scope=scope,
        claim_kind="canonical_merchant",
        canonical_name=canonical_name,
        operation_key=operation_key,
        actor=actor,
        reason=reason,
        evidence=evidence,
    )


def correct_category(
    conn: sqlite3.Connection,
    *,
    prior_claim_id: int,
    descriptor: str,
    category_id: int,
    scope: MerchantScope,
    operation_key: str,
    actor: str,
    reason: str,
    evidence: Evidence,
) -> int:
    return _correct_claim(
        conn,
        prior_claim_id=prior_claim_id,
        descriptor=descriptor,
        scope=scope,
        claim_kind="expense_category",
        category_id=category_id,
        operation_key=operation_key,
        actor=actor,
        reason=reason,
        evidence=evidence,
        provenance_kind="manual_recategorization",
    )


def current_claim(
    conn: sqlite3.Connection, claim_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM v_current_merchant_resolution_claims
        WHERE claim_id=?
        """,
        (int(claim_id),),
    ).fetchone()


def current_claims_for_descriptor(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    scope: MerchantScope,
    claim_kind: ClaimKind,
) -> list[dict[str, object]]:
    normalized = normalize_descriptor_v2(descriptor)
    rows = conn.execute(
        """
        SELECT *
        FROM v_current_merchant_resolution_claims
        WHERE normalization_version=?
          AND pattern_kind='exact_tokens'
          AND pattern_fingerprint=?
          AND household_scope=?
          AND claim_kind=?
          AND (account_id IS NULL OR account_id=?)
          AND (
            provider_identity_hash=''
            OR provider_identity_hash=?
          )
          AND (processor_family='' OR processor_family=?)
          AND (region='' OR region=?)
          AND event_kind IN ('accepted', 'corrected', 'rejected')
        ORDER BY scope_fingerprint, claim_id
        """,
        (
            NORMALIZATION_VERSION,
            normalized.fingerprint,
            scope.household_scope,
            claim_kind,
            scope.account_id,
            scope.provider_identity_hash,
            scope.processor_family,
            scope.region,
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def knowledge_digest(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT
          claim_kind, pattern_json, household_scope, account_id,
          provider_identity_hash, processor_family, region,
          COALESCE(normalized_name, '') AS normalized_name,
          COALESCE(category_name, '') AS category_name,
          COALESCE(category_id, 0) AS category_id,
          event_kind, trust_state
        FROM v_current_merchant_resolution_claims
        WHERE event_kind IN ('accepted', 'corrected', 'rejected')
        """
    ).fetchall()
    semantic_rows = {
        (
            row["claim_kind"],
            row["pattern_json"],
            row["household_scope"],
            int(row["account_id"] or 0),
            row["provider_identity_hash"],
            row["processor_family"],
            row["region"],
            row["normalized_name"],
            row["category_name"],
            int(row["category_id"] or 0),
            row["event_kind"],
            row["trust_state"],
        )
        for row in rows
    }
    return _digest(
        {
            "mapping_schema_version": MAPPING_SCHEMA_VERSION,
            "normalization_version": NORMALIZATION_VERSION,
            "claims": sorted(semantic_rows),
        }
    )
