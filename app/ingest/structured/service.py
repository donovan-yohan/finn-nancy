"""Preview and atomically confirm deterministic structured statement imports."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...accounting.contract import currency_review_reason
from ...config import get_settings
from ...db import (
    engine,
    migrate,
    repo_close,
    repo_documents,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
    repo_structured_imports,
)
from ..normalize import row_hash
from ..schemas import ExtractedStatement, StatementRow
from ..storage import blob_abspath, capture
from .csv_adapter import parse_mapped_csv
from .ofx_adapter import parse_ofx
from .pdf_adapter import PDF_VERSION, parse_pdf_statement
from .types import (
    AdapterLimits,
    ImportDiagnostic,
    ImportMetadata,
    MappedCsvV1,
    ParsedStatement,
    ParsedStatementRow,
    StructuredImportError,
)


@dataclass(frozen=True)
class PreviewResult:
    imported: sqlite3.Row
    parsed: ParsedStatement | None


@dataclass(frozen=True)
class _RowIdentity:
    row: ParsedStatementRow
    occurrence_ordinal: int
    weak_key_hash: str
    coarse_key_hash: str
    legacy_row_hash: str
    fitid_hash: str
    overlap_state: str
    supersedes_import_row_id: int | None = None
    supersedes_statement_line_id: int | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(domain: str, *parts: object) -> str:
    encoded = "\0".join((domain, *(str(part) for part in parts))).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalized_identity_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def _provider_identity_hash(
    database_identity: str | None, parsed: ParsedStatement
) -> str:
    provider_id = _normalized_identity_token(parsed.provider_id)
    account_token = _normalized_identity_token(parsed.provider_account_token)
    if not database_identity or not provider_id or not account_token:
        return ""
    return _sha256(
        "structured-provider-account-db-v1",
        database_identity,
        provider_id,
        account_token,
    )


def _adapter_identity(raw: bytes, adapter_kind: str) -> tuple[str, str]:
    if adapter_kind == "mapped_csv":
        return "mapped_csv", "mapped-csv/v1"
    if adapter_kind == "pdf":
        # The real identity is the detected institution profile, which the
        # parsed statement reports back once extraction succeeds.
        return "pdf_statement", PDF_VERSION
    if adapter_kind != "ofx":
        raise ValueError("structured adapter must be mapped_csv, ofx, or pdf")
    prefix = raw[:4096].upper()
    if b"OFXHEADER:200" in prefix or raw.lstrip().startswith(b"<?xml"):
        return "ofx_xml", "ofx-xml/v1"
    return "ofx_sgml", "ofx-sgml/v1"


def _parse(
    raw: bytes,
    *,
    adapter_kind: str,
    mapping: MappedCsvV1 | None,
    home_currency: str,
    limits: AdapterLimits,
) -> ParsedStatement:
    if adapter_kind == "mapped_csv":
        if mapping is None:
            raise ValueError("mapped CSV imports require an explicit mapping")
        return parse_mapped_csv(
            raw,
            mapping,
            home_currency=home_currency,
            limits=limits,
        )
    if adapter_kind == "ofx":
        if mapping is not None:
            raise ValueError("OFX imports do not accept a CSV mapping")
        return parse_ofx(raw, home_currency=home_currency, limits=limits)
    if adapter_kind == "pdf":
        if mapping is not None:
            raise ValueError("PDF imports do not accept a CSV mapping")
        return parse_pdf_statement(raw, home_currency=home_currency, limits=limits)
    raise ValueError("structured adapter must be mapped_csv, ofx, or pdf")


def _with_metadata(
    parsed: ParsedStatement, metadata: ImportMetadata | None
) -> ParsedStatement:
    if metadata is None:
        metadata = ImportMetadata()
    values: dict[str, Any] = {}
    manual_fields: list[str] = []
    reasons = list(parsed.review_reasons)
    for field_name in (
        "period_start_on",
        "period_end_on",
        "statement_issued_on",
        "opening_balance_cents",
        "closing_balance_cents",
    ):
        adapter_value = getattr(parsed, field_name)
        manual_value = getattr(metadata, field_name)
        manual_present = manual_value not in (None, "")
        if not manual_present:
            values[field_name] = adapter_value
            continue
        if adapter_value in (None, ""):
            values[field_name] = manual_value
            manual_fields.append(field_name)
            continue
        if (
            parsed.adapter_id == "mapped_csv"
            and field_name in {"period_start_on", "period_end_on"}
        ):
            values[field_name] = manual_value
            manual_fields.append(field_name)
            continue
        values[field_name] = adapter_value
        if manual_value != adapter_value:
            reasons.append(f"metadata_{field_name}_conflict")
    resolved = replace(
        parsed,
        **values,
        manual_fields=tuple(manual_fields),
        review_reasons=tuple(dict.fromkeys(reasons)),
    )
    if resolved.period_start_on and resolved.period_end_on:
        if any(
            row.posted_on < resolved.period_start_on
            or row.posted_on > resolved.period_end_on
            for row in resolved.rows
        ):
            resolved = replace(
                resolved,
                review_reasons=tuple(
                    dict.fromkeys(
                        (*resolved.review_reasons, "transaction_outside_period")
                    )
                ),
            )
    return resolved


def _account_review_reasons(
    account: sqlite3.Row, parsed: ParsedStatement, home_currency: str
) -> tuple[str, ...]:
    reasons = list(parsed.review_reasons)
    account_currency = str(account["currency"] or "").strip().upper()
    account_reason = currency_review_reason(account_currency, home_currency)
    if account_reason is not None:
        reasons.append(f"account_{account_reason}")
    if parsed.currency and account_currency and parsed.currency != account_currency:
        reasons.append("account_currency_mismatch")
    account_ref = re.sub(r"[^A-Za-z0-9]", "", str(account["external_ref"] or ""))
    if (
        parsed.account_last4
        and len(account_ref) >= 4
        and account_ref[-4:].casefold() != parsed.account_last4.casefold()
    ):
        reasons.append("account_identity_mismatch")
    return tuple(dict.fromkeys(reasons))


def preview_import(
    *,
    raw: bytes,
    original_name: str,
    declared_mime: str | None,
    account_id: int,
    adapter_kind: str,
    mapping: MappedCsvV1 | None = None,
    metadata: ImportMetadata | None = None,
    actor: str = "web:structured-import",
    limits: AdapterLimits | None = None,
) -> PreviewResult:
    settings = get_settings()
    limits = limits or AdapterLimits()
    if len(raw) > limits.max_bytes:
        raise StructuredImportError(
            [ImportDiagnostic("file_too_large", "The import exceeds the size limit.")]
        )
    with engine.read_conn(settings.db_path) as conn:
        account = conn.execute(
            "SELECT * FROM accounts WHERE id=?", (int(account_id),)
        ).fetchone()
        database_identity = migrate.read_database_identity(conn)
    if account is None:
        raise ValueError("structured import account not found")

    captured = capture(
        raw=raw,
        original_name=original_name,
        channel="web",
        declared_mime=declared_mime,
        source_metadata={"source": "file", "intent": "statement"},
        enqueue_ingest=False,
        forced_kind="statement",
    )
    source_document_id = int(captured["source_document_id"])
    source_sha = str(captured["sha256"])
    adapter_id, adapter_version = _adapter_identity(raw, adapter_kind)
    parsed: ParsedStatement | None = None
    provider_identity_hash = ""
    diagnostics: list[dict[str, Any]] = []
    reasons: tuple[str, ...] = ()
    try:
        parsed = _with_metadata(
            _parse(
                raw,
                adapter_kind=adapter_kind,
                mapping=mapping,
                home_currency=settings.home_currency,
                limits=limits,
            ),
            metadata,
        )
        adapter_id = parsed.adapter_id
        adapter_version = parsed.adapter_version
        reasons = _account_review_reasons(account, parsed, settings.home_currency)
        provider_identity_hash = _provider_identity_hash(
            database_identity, parsed
        )
        if parsed.provider_account_token and not provider_identity_hash:
            reasons = tuple(
                dict.fromkeys((*reasons, "provider_identity_unavailable"))
            )
        if provider_identity_hash:
            with engine.read_conn(settings.db_path) as conn:
                binding = conn.execute(
                    """SELECT account_id
                       FROM structured_provider_account_bindings
                       WHERE provider_identity_hash=?""",
                    (provider_identity_hash,),
                ).fetchone()
            if (
                binding is not None
                and int(binding["account_id"]) != int(account_id)
            ):
                reasons = tuple(
                    dict.fromkeys(
                        (*reasons, "provider_account_binding_conflict")
                    )
                )
    except StructuredImportError as exc:
        diagnostics = [item.as_dict() for item in exc.diagnostics]
        reasons = tuple(item.code for item in exc.diagnostics)

    status = "needs_review" if reasons or diagnostics else "preview_ready"
    mapping_value = mapping.as_dict() if mapping is not None else {}
    with engine.write_tx(settings.db_path) as conn:
        imported = repo_structured_imports.create_preview(
            conn,
            source_document_id=source_document_id,
            account_id=int(account_id),
            source_sha256=source_sha,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            mapping_version=mapping.version if mapping is not None else "",
            mapping=mapping_value,
            provider_identity_hash=provider_identity_hash,
            account_last4=parsed.account_last4 if parsed is not None else "",
            period_start_on=parsed.period_start_on if parsed is not None else "",
            period_end_on=parsed.period_end_on if parsed is not None else "",
            statement_issued_on=(
                parsed.statement_issued_on if parsed is not None else ""
            ),
            currency=parsed.currency if parsed is not None else "",
            opening_balance_cents=(
                parsed.opening_balance_cents if parsed is not None else None
            ),
            closing_balance_cents=(
                parsed.closing_balance_cents if parsed is not None else None
            ),
            manual_fields=parsed.manual_fields if parsed is not None else (),
            row_count=len(parsed.rows) if parsed is not None else 0,
            status=status,
            review_reasons=reasons,
            diagnostics=diagnostics,
            actor=actor,
        )
        repo_documents.set_status(conn, source_document_id, "needs_review")
    return PreviewResult(imported=imported, parsed=parsed)


def _mapping_for_import(imported: sqlite3.Row) -> tuple[str, MappedCsvV1 | None]:
    if str(imported["adapter_id"]) == "mapped_csv":
        return "mapped_csv", MappedCsvV1.from_json(str(imported["mapping_json"]))
    return "ofx", None


def _reparse(imported: sqlite3.Row) -> ParsedStatement:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        document = repo_documents.get_document(
            conn, int(imported["source_document_id"])
        )
        account = conn.execute(
            "SELECT * FROM accounts WHERE id=?", (int(imported["account_id"]),)
        ).fetchone()
        database_identity = migrate.read_database_identity(conn)
    if document is None or account is None:
        raise ValueError("structured import evidence is missing")
    path = blob_abspath(str(document["storage_ref"]))
    raw = path.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    if source_sha != str(imported["source_sha256"]):
        raise ValueError("structured import original failed SHA-256 verification")
    adapter_kind, mapping = _mapping_for_import(imported)
    parsed = _parse(
        raw,
        adapter_kind=adapter_kind,
        mapping=mapping,
        home_currency=settings.home_currency,
        limits=AdapterLimits(),
    )
    provider_hash = _provider_identity_hash(database_identity, parsed)
    if provider_hash != str(imported["provider_identity_hash"]):
        raise ValueError("structured import provider identity verification failed")
    try:
        manual_fields = tuple(
            str(value)
            for value in json.loads(str(imported["manual_fields_json"]))
            if isinstance(value, str)
        )
        preview_reasons = tuple(
            str(value)
            for value in json.loads(str(imported["review_reasons_json"]))
            if isinstance(value, str)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("structured import metadata is invalid") from exc
    parsed = replace(
        parsed,
        period_start_on=str(imported["period_start_on"] or parsed.period_start_on),
        period_end_on=str(imported["period_end_on"] or parsed.period_end_on),
        statement_issued_on=str(
            imported["statement_issued_on"] or parsed.statement_issued_on
        ),
        opening_balance_cents=(
            imported["opening_balance_cents"]
            if imported["opening_balance_cents"] is not None
            else parsed.opening_balance_cents
        ),
        closing_balance_cents=(
            imported["closing_balance_cents"]
            if imported["closing_balance_cents"] is not None
            else parsed.closing_balance_cents
        ),
        manual_fields=manual_fields,
    )
    reasons = tuple(
        dict.fromkeys(
            (
                *preview_reasons,
                *_account_review_reasons(
                    account, parsed, settings.home_currency
                ),
            )
        )
    )
    if parsed.period_start_on and parsed.period_end_on and any(
        row.posted_on < parsed.period_start_on
        or row.posted_on > parsed.period_end_on
        for row in parsed.rows
    ):
        reasons = tuple(
            dict.fromkeys((*reasons, "transaction_outside_period"))
        )
    return replace(parsed, review_reasons=reasons)


def load_preview(import_id: int) -> PreviewResult:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        imported = repo_structured_imports.get_import(conn, int(import_id))
    if imported is None:
        raise ValueError("structured import not found")
    try:
        parsed = _reparse(imported)
    except StructuredImportError:
        parsed = None
    return PreviewResult(imported=imported, parsed=parsed)


def _create_review(
    conn: sqlite3.Connection,
    *,
    imported: sqlite3.Row,
    parsed: ParsedStatement,
    actor: str,
) -> tuple[int, list[int]]:
    existing = repo_statement_reviews.get_for_document(
        conn, int(imported["source_document_id"])
    )
    if existing is not None:
        raise ValueError(
            "structured statement source already has a confirmed review"
        )

    fingerprint = _sha256(
        "structured-account-fingerprint-v1",
        int(imported["account_id"]),
        str(imported["provider_identity_hash"]),
        str(imported["account_last4"]),
    )
    activity_kind = "transactions" if parsed.rows else "zero_activity"
    cur = conn.execute(
        """INSERT INTO statement_reviews(
             source_document_id, account_id,
             period_start_on, period_end_on, statement_issued_on, period_month,
             opening_balance_cents, closing_balance_cents, currency,
             account_fingerprint, fingerprint_version, activity_kind,
             declared_row_count, observed_page_count, extracted_page_count,
             extraction_truncated, source_kind
           )
           VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?,0,0,0,'structured_rows')""",
        (
            int(imported["source_document_id"]),
            int(imported["account_id"]),
            parsed.period_start_on or None,
            parsed.period_end_on or None,
            parsed.statement_issued_on or None,
            parsed.period_month or None,
            parsed.opening_balance_cents,
            parsed.closing_balance_cents,
            parsed.currency,
            fingerprint,
            "structured-v1",
            activity_kind,
            len(parsed.rows),
        ),
    )
    review_id = int(cur.lastrowid)
    conn.execute(
        """INSERT INTO statement_review_audit(
             operation_key, statement_review_id, event_kind, actor, reason
           )
           VALUES (?,?,?,?,?)""",
        (
            f"structured:review:{int(imported['id'])}",
            review_id,
            "review_created",
            actor,
            "deterministic structured statement evidence created",
        ),
    )
    header = conn.execute(
        """INSERT INTO structured_statement_import_headers(
             import_id, statement_review_id, locator_kind, locator_json,
             source_sha256, created_by
           )
           VALUES (?,?,'structured_header',?,?,?)""",
        (
            int(imported["id"]),
            review_id,
            _json(
                {
                    "kind": "structured_header",
                    "adapter": parsed.adapter_id,
                    "import_id": int(imported["id"]),
                }
            ),
            str(imported["source_sha256"]),
            actor,
        ),
    )
    header_id = int(header.lastrowid)
    metadata_values = {
        "period_start_on": parsed.period_start_on or None,
        "period_end_on": parsed.period_end_on or None,
        "statement_issued_on": parsed.statement_issued_on or None,
        "opening_balance_cents": parsed.opening_balance_cents,
        "closing_balance_cents": parsed.closing_balance_cents,
        "currency": parsed.currency,
        "account_fingerprint": fingerprint,
    }
    for field_name, value in metadata_values.items():
        origin = "manual" if field_name in parsed.manual_fields else "extractor"
        conn.execute(
            """INSERT INTO statement_field_evidence(
                 evidence_key, statement_review_id, field_name,
                 original_value_json, confidence, structured_header_id, origin
               )
               VALUES (?,?,?,?,1.0,?,?)""",
            (
                f"structured:{int(imported['id'])}:metadata:{field_name}",
                review_id,
                field_name,
                _json(value),
                header_id,
                origin,
            ),
        )
    row_anchors: list[int] = []
    for row in parsed.rows:
        anchor = conn.execute(
            """INSERT INTO statement_source_anchors(
                 statement_review_id, locator_kind, locator_json,
                 source_sha256, created_by
               )
               VALUES (?,'raw_row',?,?,?)""",
            (
                review_id,
                _json(row.anchor),
                str(imported["source_sha256"]),
                actor,
            ),
        )
        row_anchors.append(int(anchor.lastrowid))
    return review_id, row_anchors


def _descriptor_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    tokens: list[str] = []
    pending_space = False
    for character in normalized:
        if character.isalnum():
            if pending_space and tokens:
                tokens.append(" ")
            tokens.append(character)
            pending_space = False
        else:
            pending_space = True
    return "".join(tokens).strip()


def _exact_prior_import(
    conn: sqlite3.Connection,
    *,
    imported: sqlite3.Row,
    parsed: ParsedStatement,
    signature: list[tuple[str, int]],
) -> int | None:
    candidates = conn.execute(
        """SELECT id FROM structured_statement_imports
           WHERE id<>? AND status='confirmed' AND account_id=?
             AND period_start_on IS ? AND period_end_on IS ?
             AND currency=? AND row_count=?
           ORDER BY id""",
        (
            int(imported["id"]),
            int(imported["account_id"]),
            parsed.period_start_on or None,
            parsed.period_end_on or None,
            parsed.currency,
            len(signature),
        ),
    ).fetchall()
    expected = sorted(signature)
    for candidate in candidates:
        rows = conn.execute(
            """SELECT row.weak_key_hash, row.occurrence_ordinal
               FROM structured_statement_import_rows row
               LEFT JOIN structured_statement_row_supersessions supersession
                 ON supersession.pending_import_row_id=row.id
               WHERE row.import_id=? AND supersession.id IS NULL
               ORDER BY row.id""",
            (int(candidate["id"]),),
        ).fetchall()
        actual = sorted(
            (str(row["weak_key_hash"]), int(row["occurrence_ordinal"]))
            for row in rows
        )
        if actual == expected:
            return int(candidate["id"])
    return None


def _identity_rows(
    conn: sqlite3.Connection,
    *,
    imported: sqlite3.Row,
    parsed: ParsedStatement,
) -> tuple[list[_RowIdentity], int | None]:
    occurrences: Counter[tuple[object, ...]] = Counter()
    account_id = int(imported["account_id"])
    provider_hash = str(imported["provider_identity_hash"])
    prepared: list[tuple[ParsedStatementRow, int, str, str, str, str]] = []
    fitid_counts: Counter[str] = Counter()
    for row in parsed.rows:
        key = (
            row.posted_on,
            int(row.amount_cents),
            _descriptor_identity(row.description),
            row.currency,
            int(row.is_pending),
        )
        ordinal = occurrences[key]
        occurrences[key] += 1
        weak_hash = _sha256("structured-weak-row-v2", *key)
        coarse_hash = _sha256(
            "structured-coarse-row-v1",
            row.posted_on,
            int(row.amount_cents),
            row.currency,
        )
        fitid_hash = (
            _sha256(
                "structured-fitid-v1",
                provider_hash,
                row.provider_fitid,
            )
            if provider_hash and row.provider_fitid
            else ""
        )
        if fitid_hash:
            fitid_counts[fitid_hash] += 1
        legacy_hash = row_hash(
            account_id,
            row.posted_on,
            int(row.amount_cents),
            row.description,
            ordinal,
        )
        prepared.append(
            (
                row,
                ordinal,
                weak_hash,
                coarse_hash,
                fitid_hash,
                legacy_hash,
            )
        )

    exact_import_id = _exact_prior_import(
        conn,
        imported=imported,
        parsed=parsed,
        signature=[
            (weak_hash, ordinal)
            for _, ordinal, weak_hash, _, _, _ in prepared
        ],
    )
    if exact_import_id is not None:
        return (
            [
                _RowIdentity(
                    row=row,
                    occurrence_ordinal=ordinal,
                    weak_key_hash=weak_hash,
                    coarse_key_hash=coarse_hash,
                    legacy_row_hash=legacy_hash,
                    fitid_hash=fitid_hash,
                    overlap_state="duplicate_full",
                )
                for (
                    row,
                    ordinal,
                    weak_hash,
                    coarse_hash,
                    fitid_hash,
                    legacy_hash,
                ) in prepared
            ],
            exact_import_id,
        )

    identities: list[_RowIdentity] = []
    for (
        row,
        ordinal,
        weak_hash,
        coarse_hash,
        fitid_hash,
        legacy_hash,
    ) in prepared:
        overlap = "new"
        supersedes_import_row_id: int | None = None
        supersedes_statement_line_id: int | None = None
        if fitid_hash and fitid_counts[fitid_hash] > 1:
            overlap = "ambiguous"
        elif fitid_hash:
            matched_fitid = conn.execute(
                """SELECT identity.*, line.review_disposition,
                          line.match_status, line.matched_transaction_id,
                          line.review_revision, review.id AS review_id,
                          review.revision AS review_revision,
                          review.period_month
                   FROM structured_statement_import_rows identity
                   JOIN statement_lines line
                     ON line.id=identity.statement_line_id
                   JOIN statement_reviews review
                     ON review.source_document_id=line.source_document_id
                   LEFT JOIN structured_statement_row_supersessions supersession
                     ON supersession.pending_import_row_id=identity.id
                   WHERE identity.provider_identity_hash=?
                     AND identity.fitid_hash=? AND supersession.id IS NULL
                   ORDER BY identity.is_pending, identity.id DESC
                   LIMIT 1""",
                (provider_hash, fitid_hash),
            ).fetchone()
            if matched_fitid is not None:
                can_supersede = (
                    int(matched_fitid["is_pending"]) == 1
                    and not row.is_pending
                    and str(matched_fitid["review_disposition"]) == "active"
                    and str(matched_fitid["match_status"])
                    in {"unmatched", "needs_review"}
                    and matched_fitid["matched_transaction_id"] is None
                    and not repo_close.is_month_locked(
                        conn, str(matched_fitid["period_month"] or "")
                    )
                )
                if can_supersede:
                    overlap = "supersede_pending"
                    supersedes_import_row_id = int(matched_fitid["id"])
                    supersedes_statement_line_id = int(
                        matched_fitid["statement_line_id"]
                    )
                else:
                    overlap = "ambiguous"
        if overlap == "new":
            matched_weak = conn.execute(
                """SELECT 1
                   FROM structured_statement_import_rows identity
                   LEFT JOIN structured_statement_row_supersessions supersession
                     ON supersession.pending_import_row_id=identity.id
                   WHERE identity.account_id=? AND identity.weak_key_hash=?
                     AND identity.occurrence_ordinal=?
                     AND supersession.id IS NULL
                   LIMIT 1""",
                (account_id, weak_hash, ordinal),
            ).fetchone()
            if matched_weak is not None:
                overlap = "ambiguous"
        if overlap == "new":
            matched_coarse = conn.execute(
                """SELECT 1
                   FROM structured_statement_import_rows identity
                   LEFT JOIN structured_statement_row_supersessions supersession
                     ON supersession.pending_import_row_id=identity.id
                   WHERE identity.account_id=? AND identity.coarse_key_hash=?
                     AND supersession.id IS NULL
                   LIMIT 1""",
                (account_id, coarse_hash),
            ).fetchone()
            if matched_coarse is not None:
                overlap = "ambiguous"
        if overlap == "new":
            legacy = conn.execute(
                """SELECT currency, is_pending, statement_period
                   FROM statement_lines
                   WHERE account_id=? AND row_hash=?
                     AND review_disposition='active'
                   LIMIT 1""",
                (account_id, legacy_hash),
            ).fetchone()
            if legacy is not None:
                overlap = "ambiguous"
        identities.append(
            _RowIdentity(
                row=row,
                occurrence_ordinal=ordinal,
                weak_key_hash=weak_hash,
                coarse_key_hash=coarse_hash,
                legacy_row_hash=legacy_hash,
                fitid_hash=fitid_hash,
                overlap_state=overlap,
                supersedes_import_row_id=supersedes_import_row_id,
                supersedes_statement_line_id=supersedes_statement_line_id,
            )
        )
    return identities, None


def _as_extracted(parsed: ParsedStatement) -> ExtractedStatement:
    return ExtractedStatement(
        institution=parsed.institution,
        account_last4=parsed.account_last4,
        currency=parsed.currency,
        statement_period=parsed.period_month,
        period_start_on=parsed.period_start_on,
        period_end_on=parsed.period_end_on,
        statement_issued_on=parsed.statement_issued_on,
        opening_balance_cents=parsed.opening_balance_cents,
        closing_balance_cents=parsed.closing_balance_cents,
        declared_row_count=len(parsed.rows),
        rows=[
            StatementRow(
                posted_on=row.posted_on,
                description=row.description,
                amount_cents=row.amount_cents,
                balance_cents=row.balance_cents,
                is_pending=row.is_pending,
                field_confidence={
                    "posted_on": 1.0,
                    "description": 1.0,
                    "amount_cents": 1.0,
                    "currency": 1.0,
                },
            )
            for row in parsed.rows
        ],
        confidence=1.0,
    )


def confirm_import(
    import_id: int,
    *,
    expected_revision: int,
    actor: str = "web:structured-import",
) -> sqlite3.Row:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        imported = repo_structured_imports.get_import(conn, int(import_id))
        if imported is None:
            raise ValueError("structured import not found")
        if (
            str(imported["status"]) in repo_structured_imports.TERMINAL_STATUSES
            or imported["evaluated_at"] is not None
        ):
            return imported
    parsed = _reparse(imported)

    with engine.write_tx(settings.db_path) as conn:
        current = repo_structured_imports.get_import(conn, int(import_id))
        if current is None:
            raise ValueError("structured import not found")
        if int(current["revision"]) != int(expected_revision):
            raise ValueError("structured import revision conflict")
        if parsed.period_month and repo_close.is_month_locked(
            conn, parsed.period_month
        ):
            raise repo_close.MonthLockedError(parsed.period_month)
        identities, exact_import_id = _identity_rows(
            conn, imported=current, parsed=parsed
        )
        reasons = list(parsed.review_reasons)
        binding = None
        provider_hash = str(current["provider_identity_hash"])
        if provider_hash:
            binding = conn.execute(
                """SELECT * FROM structured_provider_account_bindings
                   WHERE provider_identity_hash=?""",
                (provider_hash,),
            ).fetchone()
            if (
                binding is not None
                and int(binding["account_id"]) != int(current["account_id"])
            ):
                reasons.append("provider_account_binding_conflict")
        existing_review = repo_statement_reviews.get_for_document(
            conn, int(current["source_document_id"])
        )
        if existing_review is not None and exact_import_id is None:
            reasons.append("source_already_confirmed")

        ambiguous_count = sum(
            identity.overlap_state == "ambiguous" for identity in identities
        )
        stageable_count = sum(
            identity.overlap_state in {"new", "supersede_pending"}
            for identity in identities
        )
        supersession_count = sum(
            identity.overlap_state == "supersede_pending"
            for identity in identities
        )
        if exact_import_id is not None:
            overlap_kind = "exact"
            duplicate_count = len(identities)
        elif ambiguous_count and stageable_count:
            overlap_kind = "partial"
            duplicate_count = ambiguous_count
            reasons.append("partial_overlap")
        elif ambiguous_count:
            overlap_kind = "ambiguous"
            duplicate_count = ambiguous_count
            reasons.append("identity_conflict")
        elif supersession_count:
            overlap_kind = "supersession"
            duplicate_count = 0
        else:
            overlap_kind = "none"
            duplicate_count = 0

        reasons = list(dict.fromkeys(reasons))
        if overlap_kind == "exact" and not reasons:
            updated = repo_structured_imports.finish_import(
                conn,
                import_id=int(import_id),
                expected_revision=int(expected_revision),
                statement_review_id=None,
                status="duplicate",
                overlap_kind="exact",
                staged_count=0,
                duplicate_count=duplicate_count,
                review_reasons=(),
                actor=actor,
                reason=(
                    "one prior confirmed import has the same complete scoped "
                    "row multiset"
                ),
            )
            repo_documents.set_status(
                conn, int(current["source_document_id"]), "processed"
            )
            return updated
        if reasons:
            updated = repo_structured_imports.finish_import(
                conn,
                import_id=int(import_id),
                expected_revision=int(expected_revision),
                statement_review_id=None,
                status="needs_review",
                overlap_kind=overlap_kind,
                staged_count=0,
                duplicate_count=duplicate_count,
                review_reasons=reasons,
                actor=actor,
                reason="structured import requires review before staging",
            )
            repo_documents.set_status(
                conn, int(current["source_document_id"]), "needs_review"
            )
            return updated

        for identity in identities:
            if identity.overlap_state != "supersede_pending":
                continue
            if identity.supersedes_statement_line_id is None:
                raise RuntimeError("pending supersession lost its prior line")
            prior_line = conn.execute(
                "SELECT * FROM statement_lines WHERE id=?",
                (identity.supersedes_statement_line_id,),
            ).fetchone()
            prior_review = repo_statement_reviews.get_for_document(
                conn, int(prior_line["source_document_id"])
            )
            repo_statement_reviews.exclude_row(
                conn,
                int(prior_line["id"]),
                expected_review_revision=int(prior_review["revision"]),
                expected_line_revision=int(prior_line["review_revision"]),
                actor=actor,
                reason=(
                    f"posted successor from structured import {int(import_id)} "
                    f"row {int(identity.row.source_row_number)} replaced pending evidence"
                ),
            )

        review_id, row_anchors = _create_review(
            conn, imported=current, parsed=parsed, actor=actor
        )
        staged = repo_statements.stage_lines(
            conn,
            source_document_id=int(current["source_document_id"]),
            account_id=int(current["account_id"]),
            parsed=_as_extracted(parsed),
            row_anchor_ids=row_anchors,
        )
        staged_count = int(staged["staged"])
        if staged_count != len(parsed.rows) or int(staged["duplicates"]):
            raise RuntimeError(
                "structured import preflight diverged from atomic staging"
            )
        line_ids_by_anchor = {
            int(row["source_anchor_id"]): int(row["id"])
            for row in conn.execute(
                """SELECT id, source_anchor_id FROM statement_lines
                   WHERE source_document_id=?""",
                (int(current["source_document_id"]),),
            )
        }
        if provider_hash and binding is None:
            conn.execute(
                """INSERT INTO structured_provider_account_bindings(
                     provider_identity_hash, account_id, first_import_id,
                     verified_by, verification_reason
                   )
                   VALUES (?,?,?,?,?)""",
                (
                    provider_hash,
                    int(current["account_id"]),
                    int(import_id),
                    actor,
                    "explicit structured statement confirmation",
                ),
            )
        expectation, _ = repo_statement_expectations.attach_exact_document(
            conn,
            int(current["source_document_id"]),
            actor=actor,
            reason=(
                "deterministic structured statement identified an exact "
                "account and closing period"
            ),
        )
        expectation_linked = (
            expectation is not None
            and repo_statement_expectations.active_link_for_document(
                conn, int(current["source_document_id"])
            )
            is not None
        )
        updated = repo_structured_imports.finish_import(
            conn,
            import_id=int(import_id),
            expected_revision=int(expected_revision),
            statement_review_id=review_id,
            status="confirmed",
            overlap_kind=overlap_kind,
            staged_count=staged_count,
            duplicate_count=0,
            review_reasons=(),
            actor=actor,
            reason="structured rows staged atomically",
        )
        for index, identity in enumerate(identities):
            anchor_id = row_anchors[index]
            line_id = line_ids_by_anchor.get(anchor_id)
            if line_id is None:
                raise RuntimeError("staged structured row lost its statement line")
            inserted = conn.execute(
                """INSERT INTO structured_statement_import_rows(
                     import_id, source_row_number, source_anchor_id,
                     statement_line_id, account_id, provider_identity_hash,
                     fitid_hash, weak_key_hash, coarse_key_hash, occurrence_ordinal,
                     is_pending, currency, disposition, overlap_state
                   )
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'staged',?)""",
                (
                    int(import_id),
                    int(identity.row.source_row_number),
                    anchor_id,
                    line_id,
                    int(current["account_id"]),
                    str(current["provider_identity_hash"]),
                    identity.fitid_hash,
                    identity.weak_key_hash,
                    identity.coarse_key_hash,
                    identity.occurrence_ordinal,
                    int(identity.row.is_pending),
                    identity.row.currency,
                    identity.overlap_state,
                ),
            )
            if identity.overlap_state == "supersede_pending":
                if identity.supersedes_import_row_id is None:
                    raise RuntimeError("pending supersession lost its identity row")
                conn.execute(
                    """INSERT INTO structured_statement_row_supersessions(
                         pending_import_row_id, posted_import_row_id, actor, reason
                       )
                       VALUES (?,?,?,?)""",
                    (
                        identity.supersedes_import_row_id,
                        int(inserted.lastrowid),
                        actor,
                        "posted provider transaction replaced pending evidence",
                    ),
                )
            for field_name, value in (
                ("posted_on", identity.row.posted_on),
                ("description", identity.row.description),
                ("amount_cents", identity.row.amount_cents),
                ("currency", identity.row.currency),
                ("balance_cents", identity.row.balance_cents),
                ("is_pending", identity.row.is_pending),
            ):
                conn.execute(
                    """INSERT INTO statement_field_evidence(
                         evidence_key, statement_review_id, statement_line_id,
                         field_name, original_value_json, confidence,
                         source_anchor_id, origin
                       )
                       VALUES (?,?,?,?,?,1.0,?,'extractor')""",
                    (
                        f"structured:{int(import_id)}:row:"
                        f"{int(identity.row.source_row_number)}:{field_name}",
                        review_id,
                        line_id,
                        field_name,
                        _json(value),
                        anchor_id,
                    ),
                )
        automatically_approved = False
        completeness = repo_statement_reviews.completeness(conn, review_id)
        if (
            expectation_linked
            and not completeness["hard_blockers"]
            and not completeness["review_blockers"]
        ):
            review = repo_statement_reviews.get_review(conn, review_id)
            repo_statement_reviews.approve(
                conn,
                review_id,
                expected_revision=int(review["revision"]),
                actor=actor,
                reason=(
                    "complete deterministic structured statement "
                    "approved after explicit confirmation"
                ),
            )
            automatically_approved = True
        repo_documents.set_status(
            conn,
            int(current["source_document_id"]),
            (
                "processed"
                if automatically_approved
                else "needs_review"
            ),
        )
        return updated
