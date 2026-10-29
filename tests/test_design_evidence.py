"""Generated route/render proofs for the evidence design-system migration."""
from __future__ import annotations

import os
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import engine, repo_close, repo_documents, repo_ledger
from app.ingest.schemas import ExtractedReceipt
from tests.test_recon_ui import _insert_doc, _insert_line
from tests.test_statement_reviews import _parsed, _review
from tests.test_statement_source_view import _good_pdf, _store


class Forms(HTMLParser):
    """Native form transport projected without presentation classes or copy."""

    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action"), "method": attrs.get("method"),
                            "fields": {}, "buttons": [], "guard": attrs.get("onsubmit")}
            self.forms.append(self.current)
        elif self.current is not None:
            if tag in {"input", "select", "textarea"} and attrs.get("name"):
                self.current["fields"][attrs["name"]] = attrs
            if tag == "button":
                self.current["buttons"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None

    def at(self, action, **values):
        return next(form for form in self.forms if form["action"] == action and all(
            form["fields"][key].get("value") == value for key, value in values.items()))


def seed_evidence(db_path):
    """All generated. No providers, original-data reads, worker or model calls."""
    doc, review_id = _review(db_path, _parsed())
    with engine.write_tx(db_path) as conn:
        repo_documents.set_status(conn, doc, "needs_review")
        receipt = ExtractedReceipt(merchant="Generated market", purchased_on="2026-06-12",
                                   total_cents=1234, currency="CAD", confidence=.2,
                                   category_guess="Uncategorized")
        receipt_doc = repo_documents.insert_source_document(
            conn, kind="receipt", original_name="generated-receipt.txt",
            storage_ref="generated-receipt.txt", sha256="a" * 64,
            mime_type="text/plain", status="needs_review")
        extraction = repo_documents.insert_extraction(
            conn, source_document_id=receipt_doc, doc_kind="receipt",
            extracted_json=receipt.model_dump_json(), confidence=.2, external_id="",
            proposed_account_id=None, proposed_category_id=None, review_status="pending")
        account = conn.execute("SELECT account_id FROM statement_reviews WHERE id=?",
                               (review_id,)).fetchone()[0]
        category = repo_ledger.ensure_uncategorized(conn)
        candidate = repo_ledger.insert_transaction(
            conn, account_id=account, posted_on="2026-06-10", description="Generated purchase",
            counterparty="Generated market", amount_cents=-5000, source="manual",
            external_id="design-evidence-purchase", source_document_id=None,
            source_confidence=1, flow_kind="purchase")
        assert candidate is not None
        repo_ledger.insert_split(conn, transaction_id=candidate, category_id=category,
                                 amount_cents=-5000)
    positive_doc = _insert_doc(name="generated-credit.csv")
    negative = _insert_line(doc_id=positive_doc, account_id=account, posted_on='2026-06-10',
                            amount_cents=-5000, description='Generated purchase', row_hash='design-match')
    positive = _insert_line(doc_id=positive_doc, account_id=account, posted_on="2026-06-20",
                            amount_cents=2500, description="Generated credit", row_hash="design-credit")
    return {"statement": doc, "review": review_id, "receipt": receipt_doc,
            "extraction": extraction, "positive_line": positive, "account": account,
            "negative_line": negative, "candidate": candidate}


@pytest.fixture
def evidence_app(empty_db, tmp_path, monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STRICT_LOCAL_MODE", "true")
    monkeypatch.setenv("READ_ONLY", "false")
    monkeypatch.setenv("HOME_CURRENCY", "CAD")
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    from app.web.app import create_app
    client = TestClient(create_app())  # No lifespan, worker, model warm-up or provider.
    yield client, empty_db
    get_settings.cache_clear()


def save_render(name, html):
    directory = os.getenv("DESIGN_EVIDENCE_ARTIFACT_DIR")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(html)


def assert_foundation(html):
    assert '/static/foundation/vendor/bootstrap.min.css' in html
    assert '/static/foundation/evidence.css' in html
    assert '/static/app.css' not in html
    assert html.count('<h1>') == 1


@pytest.mark.parametrize("route,copy", [('/review', 'No items need review.'),
                                          ('/recon', 'No lines need reconciliation.')])
def test_empty_evidence_destinations(evidence_app, route, copy):
    client, _ = evidence_app
    response = client.get(route)
    assert response.status_code == 200
    assert_foundation(response.text)
    assert copy in response.text
    save_render(route.strip('/') + '-empty.html', response.text)


def test_receipt_decisions_keep_native_fields_and_confirmations(evidence_app):
    client, db = evidence_app
    ids = seed_evidence(db)
    response = client.get('/review')
    assert_foundation(response.text)
    forms = Forms(response.text)
    approval = forms.at(f'/review/{ids["extraction"]}/approve')
    assert set(approval['fields']) == {'merchant', 'purchased_on', 'total',
                                       'category_name', 'account_id'}
    assert approval['fields']['total']['value'] == '12.34'
    assert approval['fields']['merchant']['class'] == 'form-control'
    assert approval['fields']['category_name']['class'] == 'form-select'
    assert 'data-resolution-kind="canonical_merchant"' in response.text
    assert 'data-resolution-kind="expense_category"' in response.text
    for action in [f'/review/{ids["extraction"]}/reject', f'/review/doc/{ids["receipt"]}/delete']:
        assert 'confirm(' in forms.at(action)['guard']
    assert f'/review/statement/{ids["statement"]}' in response.text
    save_render('review-populated.html', response.text)


def test_statement_blocker_revision_and_restore_paths(evidence_app):
    client, db = evidence_app
    ids = seed_evidence(db)
    doc = ids['statement']
    page = client.get(f'/review/statement/{doc}')
    assert_foundation(page.text)
    forms = Forms(page.text)
    assert forms.at(f'/review/statement/{doc}/metadata')['fields']['expected_revision']['value'] == '1'
    with engine.read_conn(db) as conn:
        line = conn.execute('SELECT id FROM statement_lines WHERE source_document_id=?', (doc,)).fetchone()[0]
    exclude = forms.at(f'/review/statement/{doc}/row/{line}/exclude')
    payload = {key: attrs.get('value', '') for key, attrs in exclude['fields'].items()}
    payload['reason'] = 'Generated duplicate source row'
    changed = client.post(exclude['action'], data=payload, follow_redirects=False)
    assert changed.status_code == 303
    after = client.get(changed.headers['location'])
    restore = Forms(after.text).at(f'/review/statement/{doc}/row/{line}/restore')
    assert restore['fields']['expected_review_revision']['value'] == '1'
    assert restore['fields']['expected_line_revision']['value'] == '2'
    stale = client.post(exclude['action'], data=payload, follow_redirects=False)
    assert stale.status_code == 409
    with engine.write_tx(db) as conn:
        conn.execute("UPDATE accounts SET currency='USD' WHERE id=?", (ids['account'],))
    blocked = client.get(f'/review/statement/{doc}')
    approval = Forms(blocked.text).at(f'/review/statement/{doc}/approve')
    assert 'disabled' in approval['buttons'][0]
    assert approval['buttons'][0]['aria-disabled'] == 'true'
    assert 'account currency mismatch' in blocked.text
    assert f'/review/doc/{doc}/file#page=1' in blocked.text
    save_render('statement-review-populated.html', page.text)
    save_render('statement-review-blocked.html', blocked.text)


def test_positive_decision_uses_rendered_authority_and_exact_undo(evidence_app):
    client, db = evidence_app
    ids = seed_evidence(db)
    assert client.post(f'/recon/line/{ids["positive_line"]}/promote',
                       data={'month': '2026-06', 'flow_kind': 'unknown'},
                       follow_redirects=False).status_code == 303
    page = client.get('/recon?month=2026-06')
    assert_foundation(page.text)
    assert page.text.index('<h2>Money in to explain</h2>') < page.text.index('<h2>Statement lines</h2>') < page.text.index('<h2>Matched vs unmatched spend</h2>')
    assert 'tabindex="0" role="region" aria-label="Match candidates"' in page.text
    forms = Forms(page.text)
    accept = next(form for form in forms.forms if form['action'].endswith('/accept-classification')
                  and form['fields']['flow_kind']['value'] == 'income')
    payload = {key: attrs['value'] for key, attrs in accept['fields'].items()}
    assert payload['evidence_fingerprint'] and payload['operation_key']
    rejected = client.post(accept['action'], data={**payload, 'evidence_fingerprint': 'stale'}, follow_redirects=False)
    assert rejected.status_code == 303
    assert 'No changes made' in client.get(rejected.headers['location']).text
    assert client.post(accept['action'], data=payload, follow_redirects=False).status_code == 303
    accepted = client.get('/recon?month=2026-06')
    undo = next(form for form in Forms(accepted.text).forms if form['action'].endswith('/undo'))
    assert 'confirm(' in undo['guard']
    undo_payload = {key: attrs['value'] for key, attrs in undo['fields'].items()}
    assert client.post(undo['action'], data=undo_payload, follow_redirects=False).status_code == 303
    with engine.read_conn(db) as conn:
        transaction = conn.execute('SELECT t.flow_kind,t.amount_cents FROM transactions t JOIN statement_lines l ON l.matched_transaction_id=t.id WHERE l.id=?', (ids['positive_line'],)).fetchone()
        assert tuple(transaction) == ('unknown', 2500)
    save_render('recon-proposals.html', page.text)
    save_render('recon-resolved.html', accepted.text)


def test_read_only_request_keeps_evidence_readable_but_blocks_receipt_attach(evidence_app, monkeypatch):
    client, db = evidence_app
    ids = seed_evidence(db)
    monkeypatch.setenv('READ_ONLY', 'true')
    get_settings.cache_clear()
    assert client.get('/review').status_code == 200
    response = client.post(f'/review/{ids["extraction"]}/attach',
                           data={'transaction_id': '1', 'reason': 'Generated read-only probe'},
                           follow_redirects=False)
    assert response.status_code == 403
    with engine.read_conn(db) as conn:
        assert conn.execute('SELECT matched_transaction_id FROM statement_lines WHERE id=?',
                            (ids['positive_line'],)).fetchone()[0] is None


def test_pdf_source_keeps_reciprocal_anchor_and_normalized_geometry(evidence_app):
    client, db = evidence_app
    doc = _store(db, _good_pdf(), name='generated-source.pdf')
    page = client.get(f'/review/pdf/{doc}')
    assert_foundation(page.text)
    for number in range(1, 5):
        assert f'id="row-{number}"' in page.text
        assert f'href="#region-{number}-0"' in page.text
        assert f'href="#row-{number}"' in page.text
    assert 'tabindex="0" role="region" aria-label="Source pages"' in page.text
    assert '--x0:' in page.text and '--aspect:' in page.text
    assert '/static/evidence-lens.js' in page.text
    save_render('statement-source-populated.html', page.text)


def test_equal_amount_receipt_retains_attach_and_separate_purchase(evidence_app):
    client, db = evidence_app
    ids = seed_evidence(db)
    with engine.write_tx(db) as conn:
        category = repo_ledger.ensure_uncategorized(conn)
        candidate = repo_ledger.insert_transaction(
            conn, account_id=ids['account'], posted_on='2026-06-12',
            description='Generated equal-amount statement purchase', counterparty='',
            amount_cents=-1234, source='statement', external_id='design-duplicate',
            source_document_id=ids['statement'], source_confidence=1, flow_kind='purchase')
        assert candidate is not None
        repo_ledger.insert_split(conn, transaction_id=candidate, category_id=category,
                                 amount_cents=-1234)
    page = client.get('/review')
    forms = Forms(page.text)
    attach = forms.at(f'/review/{ids["extraction"]}/attach')
    assert set(attach['fields']) == {'transaction_id', 'reason'}
    separate = forms.at(f'/review/{ids["extraction"]}/approve')
    assert separate['fields']['acknowledge_separate_purchase']['value'] == 'true'
    assert 'separate_purchase_reason' in separate['fields']
    assert 'Equal amounts are candidates, not proof of a duplicate.' in page.text
    save_render('review-equal-amount.html', page.text)


def test_closed_period_statement_correction_preserves_source(evidence_app):
    client, db = evidence_app
    ids = seed_evidence(db)
    doc = ids['statement']
    page = client.get(f'/review/statement/{doc}')
    form = Forms(page.text).at(f'/review/statement/{doc}/archive')
    with engine.write_tx(db) as conn:
        repo_close.mark_closed(conn, '2026-06', reason='Generated period lock')
    response = client.post(form['action'], data={'reason': 'Generated archive attempt'},
                           follow_redirects=False)
    assert response.status_code == 409
    assert '2026-06' in response.text
    with engine.read_conn(db) as conn:
        document = repo_documents.get_document(conn, doc)
        assert document is not None and document['status'] == 'needs_review'
        assert conn.execute('SELECT review_disposition FROM statement_lines WHERE source_document_id=?',
                            (doc,)).fetchone()[0] == 'active'


def test_explicit_zero_activity_review_keeps_approval_and_source(evidence_app):
    from tests.test_statement_reviews import _zero_activity_parsed
    client, db = evidence_app
    doc, _ = _review(db, _zero_activity_parsed(), source_suffix='-zero')
    page = client.get(f'/review/statement/{doc}')
    assert_foundation(page.text)
    assert 'No extracted rows.' in page.text
    assert 'Explicitly no transactions' in page.text
    assert Forms(page.text).at(f'/review/statement/{doc}/approve')
    assert f'/review/doc/{doc}/file#page=1' in page.text
    save_render('statement-zero-activity.html', page.text)
