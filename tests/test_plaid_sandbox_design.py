"""Candidate presentation contracts; synthetic records, no provider or live store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

from app.plaid_sandbox.web import create_app
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app/web/static"
TEMPLATES = ROOT / "app/web/templates"


def render_rows(*, overview=None, **changes):
    if not shutil.which("node"):
        pytest.skip("Node is required to exercise the candidate row seam")
    record = {
        "name": "Generated <merchant>", "account_name": "Generated Checking",
        "owner_id": "alpha", "connection_id": "generated-connection",
        "account_id": "same-account", "date": "2026-01-03", "state": "pending",
        "selected": False, "amount_cents": "-1234", "currency": "CAD",
        "ownership": "shared", "transaction_id": "generated-transaction",
    }
    record.update(changes)
    if overview is None:
        overview = {
            "members": [{"id": "alpha", "name": "Generated Alpha"}, {"id": "beta", "name": "Generated Beta"}],
            "connections": [{"id": "generated-connection", "owner_id": "alpha"}],
            "accounts": [],
        }
    # Exercise the real row seam and textContent assignment without a browser dependency.
    harness = r"""
const fs = require('node:fs');
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.className = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(n => n.textContent).join(''); }
  get classList() { return {add: value => {this.className += ' ' + value;}}; }
  append(...nodes) { this.children.push(...nodes); }
  set innerHTML(value) { throw Error('Unsafe HTML rendering'); }
}
const document = {getElementById: () => null, querySelector: () => new Node('main'),
  createElement: tag => new Node(tag)};
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('  const $ =');
const end = source.indexOf('  function renderHome()');
const record = JSON.parse(process.argv[2]);
const overview = JSON.parse(process.argv[3]);
const before = JSON.stringify([record, overview]);
const result = new Function('document', 'record', 'overview', source.slice(start, end) + `
state = overview;
return [row(record), row(record, true)];`)(document, record, overview);
if (JSON.stringify([record, overview]) !== before) throw Error('Rendering changed source records');
console.log(JSON.stringify(result));
"""
    result = subprocess.run(
        ["node", "-e", harness, str(STATIC / "plaid-sandbox.js"), json.dumps(record), json.dumps(overview)],
        check=True, capture_output=True, text=True, cwd=ROOT,
    )
    return json.loads(result.stdout)


def descendants(node):
    yield node
    for child in node.get("children", []):
        yield from descendants(child)


def slot(node, name):
    return next(n for n in descendants(node) if name in n.get("className", "").split())


def text(node):
    return node.get("text", "") + "".join(text(child) for child in node.get("children", []))


def facts(node):
    return {
        text(label): value
        for listing in descendants(node) if listing["tag"] == "dl"
        for label, value in zip(listing["children"][::2], listing["children"][1::2])
    }


def test_rows_have_named_identity_columns_and_native_navigation():
    activity, recent = render_rows()
    assert activity["tag"] == "details"
    summary = activity["children"][0]
    assert summary["tag"] == "summary"
    assert recent["tag"] == "a"
    assert recent["href"] == "/activity?connection_id=generated-connection&account_id=same-account"
    for row in (summary, recent):
        assert text(slot(row, "sandbox-merchant")) == "Generated <merchant>"
        assert "Generated Checking" in text(slot(row, "sandbox-account-label"))
        assert "Generated Alpha" in text(slot(row, "sandbox-account-label"))
        assert text(slot(row, "sandbox-date")) == "2026-01-03"
        assert "Pending" in text(slot(row, "sandbox-state"))
        assert "Excluded" in text(slot(row, "sandbox-state"))
        assert text(slot(row, "sandbox-money")) == "−12.34 CAD"
        assert slot(row, "sandbox-chevron")["tag"] == "span"
    assert "Unknown · not booked" in text(activity)
    assert "Source details" in text(activity)
    assert "generated-transaction" in text(activity)


@pytest.mark.parametrize(("name", "enrichment", "expected"), [
    ("Legacy description", {"merchant_name": "Normalized grocer"}, "Normalized grocer"),
    ("Legacy description", {"merchant_name": "  Exact merchant  "}, "  Exact merchant  "),
    ("Legacy description", {"merchant_name": " \t "}, "Legacy description"),
    ("Legacy description", {"merchant_name": {"name": "Do not coerce"}}, "Legacy description"),
    ("Legacy description", {"merchant_name": ["Do not coerce"]}, "Legacy description"),
    ("Legacy description", {"merchant_name": 42}, "Legacy description"),
    ("Legacy description", None, "Legacy description"),
    ("Legacy description", [], "Legacy description"),
    ("Legacy description", "invalid", "Legacy description"),
    ("", {"counterparties": [{"name": "Never infer merchant"}]}, "Unnamed transaction"),
    (" \n ", {}, "Unnamed transaction"),
    ({"name": "Do not coerce"}, {}, "Unnamed transaction"),
    (["Do not coerce"], {}, "Unnamed transaction"),
    (True, {}, "Unnamed transaction"),
    (None, {}, "Unnamed transaction"),
])
def test_row_and_home_prefer_only_valid_merchant_strings(name, enrichment, expected):
    for row in render_rows(name=name, enrichment=enrichment):
        assert text(slot(row, "sandbox-merchant")) == expected
        assert text(slot(row, "sandbox-money")) == "−12.34 CAD"
        assert text(slot(row, "sandbox-account-label")) == "Generated Checking · Generated Alpha"


@pytest.mark.parametrize("original", [None, "", " \t ", {}, [], 42, True,
                                      "  BANK / 0089\n<original> & exact  "])
def test_original_bank_description_is_exact_or_honestly_not_supplied(original):
    activity, recent = render_rows(name="Legacy provider description", enrichment={"original_description": original})
    values = facts(activity)
    assert text(values["Merchant"]) == "Not supplied", "a legacy description is not a supplied merchant"
    assert text(values["Provider description"]) == "Legacy provider description"
    assert text(values["Original bank description"]) == (
        original if isinstance(original, str) and original.strip() else "Not supplied"
    )
    source = next(n for n in descendants(activity) if n["tag"] == "details" and n is not activity)
    assert text(source["children"][0]) == "Source details"
    assert "Provider description" in facts(source) and "Original bank description" in facts(source)
    assert "Original bank description" not in text(recent)


@pytest.mark.parametrize("enrichment", [None, {}, [], "invalid"])
def test_missing_enrichment_keeps_legacy_description_without_inventing_original(enrichment):
    activity, _ = render_rows(enrichment=enrichment)
    assert text(facts(activity)["Provider description"]) == "Generated <merchant>"
    assert text(facts(activity)["Original bank description"]) == "Not supplied"


def test_merchant_and_source_descriptions_are_separate_text_only_evidence():
    merchant = '<img src="https://untrusted.invalid/logo" onerror="alert(1)">'
    provider = '<a href="https://untrusted.invalid">PROVIDER & 0007</a>'
    original = '  BANK\n<script>alert("original")</script> & exact  '
    activity, recent = render_rows(name=provider, enrichment={
        "merchant_name": merchant, "original_description": original,
        "logo_url": "https://untrusted.invalid/logo", "website": "https://untrusted.invalid",
    })
    values = facts(activity)
    assert text(values["Merchant"]) == merchant
    assert text(values["Provider description"]) == provider
    assert text(values["Original bank description"]) == original
    assert "Unknown · not booked" in text(activity)
    assert len(activity["children"][0]["children"]) == len(recent["children"]) == 5
    assert all(n["tag"] not in {"a", "img", "script", "iframe", "p"} for n in descendants(activity))
    assert all(n["tag"] not in {"img", "script", "iframe", "p"} for n in descendants(recent))
    assert text(slot(recent, "sandbox-merchant")) == merchant
    assert recent["href"] == "/activity?connection_id=generated-connection&account_id=same-account"
    for scan in (activity["children"][0], recent):
        assert provider not in text(scan) and original not in text(scan)


@pytest.mark.parametrize(("category", "expected"), [
    ({"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES",
      "confidence_level": "VERY_HIGH", "version": "v2"},
     "Suggested by Plaid · Food and drink · Food and drink groceries · Confidence: Very high · Version: v2"),
    ({"primary": "GENERAL_MERCHANDISE"}, "Suggested by Plaid · General merchandise"),
    ({"detailed": "FOOD_AND_DRINK_GROCERIES", "confidence_level": {}, "version": 7},
     "Suggested by Plaid · Food and drink groceries"),
    ({"primary": '<img src=x onerror="alert(1)">', "version": '<script>alert("v")</script>'},
     'Suggested by Plaid · <img src=x onerror="alert(1)"> · Version: <script>alert("v")</script>'),
])
def test_category_is_readable_orange_provider_suggestion_only_in_expanded_facts(category, expected):
    activity, recent = render_rows(enrichment={"category_suggestion": category})
    suggestion = facts(activity)["Category suggestion"]
    assert text(suggestion) == expected
    assert "nancy" in suggestion["className"].split()
    assert suggestion in activity["children"][1]["children"], "suggestion stays behind the native disclosure"
    for scan in (activity["children"][0], recent):
        assert "Suggested by Plaid" not in text(scan)
        assert not any("nancy" in n.get("className", "").split() for n in descendants(scan))
        assert len(scan["children"]) == 5
    assert "Unknown · not booked" in text(activity)
    assert all(n["tag"] not in {"a", "img", "script", "iframe", "button", "p"} for n in descendants(activity))


@pytest.mark.parametrize("category", [None, {}, [], "FOOD", True,
                                      {"primary": [], "detailed": {}, "confidence_level": "HIGH", "version": "v2"},
                                      {"primary": " \t ", "detailed": 23}])
def test_malformed_or_missing_category_does_not_manufacture_a_suggestion(category):
    activity, recent = render_rows(enrichment={"category_suggestion": category})
    assert "Category suggestion" not in facts(activity)
    assert "Suggested by Plaid" not in text(activity) + text(recent)


def test_counterparty_identities_stay_distinct_subordinate_provider_evidence():
    activity, recent = render_rows(name="Provider marketplace charge", enrichment={
        "merchant_name": "Actual grocer", "merchant_entity_id": "entity-merchant-exact",
        "counterparties": [
            {"name": "Marketplace", "entity_id": "entity-market", "type": "marketplace", "confidence_level": "HIGH"},
            {"name": "Actual grocer", "entity_id": "entity-seller", "type": "merchant", "confidence_level": "VERY_HIGH"},
            {"name": "Payment service", "entity_id": "entity-payment", "type": "payment_app", "confidence_level": "LOW"},
        ],
    })
    source = next(n for n in descendants(activity) if n["tag"] == "details" and n is not activity)
    values = facts(source)
    assert text(values["Plaid merchant entity ID"]) == "entity-merchant-exact"
    assert text(values["Plaid counterparty 1"]) == "Marketplace · Type: marketplace · Entity ID: entity-market · Confidence: HIGH"
    assert text(values["Plaid counterparty 2"]) == "Actual grocer · Type: merchant · Entity ID: entity-seller · Confidence: VERY_HIGH"
    assert text(values["Plaid counterparty 3"]) == "Payment service · Type: payment_app · Entity ID: entity-payment · Confidence: LOW"
    assert text(facts(activity)["Merchant"]) == "Actual grocer"
    assert "Unknown · not booked" in text(activity)
    for scan in (activity["children"][0], recent):
        assert text(slot(scan, "sandbox-merchant")) == "Actual grocer"
        assert "Marketplace" not in text(scan) and "entity-" not in text(scan)
    assert all(n["tag"] not in {"img", "a", "script", "p", "button"} for n in descendants(source))


@pytest.mark.parametrize("counterparties", [None, {}, "invalid", True, [None, [], {}, "bad", 42]])
def test_malformed_counterparties_cannot_supply_a_merchant_or_coerced_metadata(counterparties):
    activity, recent = render_rows(name={}, enrichment={
        "merchant_name": [], "merchant_entity_id": {}, "counterparties": counterparties,
    })
    assert text(facts(activity)["Merchant"]) == "Not supplied"
    assert text(facts(activity)["Provider description"]) == "Not supplied"
    assert not any(label.startswith("Plaid counterparty") or label == "Plaid merchant entity ID" for label in facts(activity))
    for scan in (activity["children"][0], recent):
        assert text(slot(scan, "sandbox-merchant")) == "Unnamed transaction"
    assert "[object Object]" not in text(activity) and "undefined" not in text(activity)


def test_partial_counterparty_fields_remain_text_only_without_invented_names():
    attack = '<img src="https://untrusted.invalid/counterparty" onerror="alert(1)">'
    activity, recent = render_rows(name=None, enrichment={
        "merchant_entity_id": '<script>alert("entity")</script>',
        "counterparties": [None, {}, {"name": [], "type": "marketplace", "entity_id": "exact-entity", "confidence_level": {}},
                           {"name": attack, "type": {}, "entity_id": [], "confidence_level": "LOW"}],
    })
    values = facts(activity)
    assert text(values["Plaid merchant entity ID"]) == '<script>alert("entity")</script>'
    assert text(values["Plaid counterparty 1"]) == "Type: marketplace · Entity ID: exact-entity"
    assert text(values["Plaid counterparty 2"]) == attack + " · Confidence: LOW"
    assert text(values["Merchant"]) == "Not supplied"
    assert all(n["tag"] not in {"img", "script", "a"} for n in descendants(activity))
    assert all(text(slot(scan, "sandbox-merchant")) == "Unnamed transaction" for scan in (activity, recent))


def test_same_owner_connection_collisions_have_stable_human_labels_in_activity_and_recent():
    overview = {
        "members": [{"id": "alpha", "name": "Generated Alpha"}, {"id": "beta", "name": "Generated Beta"}],
        "connections": [{"id": "generated-connection", "owner_id": "alpha"},
                        {"id": "generated-other", "owner_id": "alpha"},
                        {"id": "generated-beta", "owner_id": "beta"}],
        "accounts": [{"connection_id": connection, "account_id": "same-account", "name": "Generated Checking"}
                     for connection in ("generated-connection", "generated-other", "generated-beta")],
    }
    # Rendering/filter order must not renumber connection identities from overview order.
    for connection, owner, suffix in (("generated-other", "alpha", " · Connection 2"),
                                      ("generated-beta", "beta", ""),
                                      ("generated-connection", "alpha", " · Connection 1")):
        activity, recent = render_rows(overview=overview, connection_id=connection, owner_id=owner)
        expected = "Generated Checking · Generated " + ("Beta" if owner == "beta" else "Alpha") + suffix
        for row in (activity, recent):
            assert text(slot(row, "sandbox-account-label")) == expected
            assert text(slot(row, "sandbox-money")) == "−12.34 CAD"
        assert recent["href"] == f"/activity?connection_id={connection}&account_id=same-account"
        assert "Unknown · not booked" in text(activity)
        assert connection in text(activity), "raw identifiers remain available only in source evidence"


@pytest.mark.parametrize("across_connections", [False, True])
def test_duplicate_names_within_one_connection_get_stable_account_suffixes(across_connections):
    overview = {
        "members": [{"id": "alpha", "name": "Generated Alpha"}],
        "connections": [{"id": "generated-connection", "owner_id": "alpha"}],
        "accounts": [{"connection_id": "generated-connection", "account_id": account, "name": name}
                     for account, name in (("unrelated", "Generated Savings"),
                                           ("same-account", "Generated Checking"),
                                           ("other-account", "Generated Checking"))],
    }
    if across_connections:
        overview["connections"].append({"id": "generated-other", "owner_id": "alpha"})
        overview["accounts"].append({"connection_id": "generated-other", "account_id": "same-account", "name": "Generated Checking"})
    for account, number in (("other-account", 3), ("same-account", 2)):
        for row in render_rows(overview=overview, account_id=account):
            assert text(slot(row, "sandbox-account-label")) == (
                "Generated Checking · Generated Alpha" + (" · Connection 1" if across_connections else "")
                + f" · Account {number}"
            )
    for row in render_rows(overview=overview, account_id="unrelated", account_name="Generated Savings"):
        assert text(slot(row, "sandbox-account-label")) == "Generated Savings · Generated Alpha"


@pytest.mark.parametrize(("changes", "expected"), [
    ({"amount_cents": "-8999999999999999"}, "−89,999,999,999,999.99 CAD"),
    ({"amount_cents": "2001", "currency": None}, "+20.01 Unknown currency"),
    ({"amount_cents": "0"}, "0.00 CAD"),
    ({"amount_cents": "-1", "state": "removed"}, "−0.01 CAD"),
])
def test_row_preserves_signed_cents_currency_and_source_state(changes, expected):
    for row in render_rows(**changes):
        assert text(slot(row, "sandbox-money")) == expected
        if changes.get("state") == "removed":
            assert "Removed" in text(slot(row, "sandbox-state"))


def test_activity_toolbar_and_column_labels_are_compact_without_duplicate_heading(tmp_path):
    with TestClient(create_app(tmp_path / "candidate", port=8772), base_url="http://127.0.0.1:8772") as client:
        page = client.get("/activity").text
    assert 'maxlength="120"' in page
    assert 'maxlength="200"' not in page
    assert '>Apply</button>' in page
    assert 'class="sandbox-activity-toolbar"' in page
    assert page.count('Sandbox · test data') == 1
    assert '>Not booked<' in page
    assert 'Imported activity</h2>' not in page
    assert 'sandbox-column-head' in page
    for label in ('Merchant', 'Account', 'Date', 'Status', 'Signed amount'):
        assert f'>{label}</span>' in page


def test_summary_pages_remove_repeated_narration_but_keep_consent_and_recovery(tmp_path):
    with TestClient(create_app(tmp_path / "candidate", port=8772), base_url="http://127.0.0.1:8772") as client:
        home, accounts, connections = [client.get(route).text for route in ("/", "/accounts", "/connections")]
    assert "Import overview</h2>" not in home
    assert "Imported accounts</h2>" not in accounts
    assert "Connection status</h2>" not in connections
    assert 'id="sandbox-summary"' in home
    assert 'id="sandbox-accounts"' in accounts
    assert 'id="sandbox-connections"' in connections
    for guidance in ("never real bank credentials", "I am this member", "plaid-sandbox.v1", "Link recovery", "Disable network access"):
        assert guidance in connections
    source = (STATIC / "plaid-sandbox.js").read_text()
    assert "state.enabled ? '' : 'Disabled" in source
    assert "edit.dataset.action = 'edit-account'" in source
    assert "button('Cancel'" in source and "Save changes" in source
    assert "Last local sync: " in source
    assert "Never replay this exchange" in source


def test_rendered_rows_toolbar_and_expanded_evidence_fit_phone_and_desktop(tmp_path):
    chrome = os.environ.get("CHROMIUM_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not Path(chrome).is_file() and not shutil.which(chrome):
        pytest.skip("Set CHROMIUM_BIN to exercise rendered candidate geometry")
    process = subprocess.Popen([
        chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-background-networking",
        "--remote-debugging-port=0", f"--user-data-dir={tmp_path / 'chrome'}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = tmp_path / "chrome/DevToolsActivePort"
        for _ in range(100):
            if port_file.exists():
                break
            time.sleep(.05)
        import urllib.request
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list") as response:
            target = next(item["webSocketDebuggerUrl"] for item in json.load(response) if item["type"] == "page")
        with connect(target) as socket:
            ordinal = 0

            def call(method, params=None):
                nonlocal ordinal
                ordinal += 1
                socket.send(json.dumps({"id": ordinal, "method": method, "params": params or {}}))
                while True:
                    result = json.loads(socket.recv())
                    if result.get("id") == ordinal:
                        assert "error" not in result, result
                        return result.get("result", {})

            def evaluate(expression):
                result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                assert "exceptionDetails" not in result, result
                return result["result"].get("value")

            with TestClient(create_app(tmp_path / "candidate", port=8772), base_url="http://127.0.0.1:8772") as client:
                html = client.get("/activity").text
            html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html)
            html = re.sub(r"<link\b[^>]*>", "", html)
            styles = "\n".join((STATIC / "foundation" / name).read_text() for name in (
                "vendor/bootstrap.min.css", "tokens.css", "components.css", "banking.css",
            ))
            html = html.replace("</head>", f"<style>{styles}</style></head>")
            call("Page.enable")
            frame = call("Page.getFrameTree")["frameTree"]["frame"]["id"]
            call("Page.setDocumentContent", {"frameId": frame, "html": html})
            source = (STATIC / "plaid-sandbox.js").read_text()
            row_code = source[source.index("  const $ ="):source.index("  function renderHome()")]
            evaluate("(()=>{" + row_code + """
state = {members: [{id: 'alpha', name: 'Generated member Alpha'}]};
const record = {name: 'Generated merchant 114', account_name: 'Generated Checking', owner_id: 'alpha',
date: '2026-01-03', state: 'posted', selected: true, amount_cents: '-1234', currency: 'CAD',
enrichment: {merchant_name: 'Normalized grocer', merchant_entity_id: 'generated-merchant-entity',
original_description: '  BANK / 0089\\n<img src="https://untrusted.invalid/original" onerror="alert(1)"> & exact  ',
category_suggestion: {primary:'FOOD_AND_DRINK', detailed:'FOOD_AND_DRINK_GROCERIES', confidence_level:'VERY_HIGH',version:'v2'},
counterparties:[{name:'Generated marketplace',type:'marketplace',entity_id:'generated-marketplace',confidence_level:'HIGH'}]}};
document.querySelector('#sandbox-staged').replaceChildren(row(record), row({...record, state: 'pending', selected: false}),
row({...record, amount_cents: '-8999999999999999'}));
document.querySelector('#sandbox-status').textContent = '';
document.querySelector('#sandbox-result-count').textContent = '1–3 of 3 transactions';
})()""")
            assert evaluate("""(()=>{const row=document.querySelector('.sandbox-transaction');
const suggestion=row.querySelector('.nancy');
return !row.open && suggestion.getClientRects().length===0
&& row.querySelector('.sandbox-merchant').textContent==='Normalized grocer'
&& !row.querySelector('summary').textContent.includes('Suggested by Plaid')
&& document.querySelectorAll('#sandbox-staged img,#sandbox-staged script,#sandbox-staged a,#sandbox-staged iframe').length===0;})()""")
            assert evaluate("JSON.stringify([...document.querySelectorAll('.sandbox-transaction > summary .sandbox-money')].map(n=>n.textContent))===JSON.stringify(['−12.34 CAD','−12.34 CAD','−89,999,999,999,999.99 CAD'])")
            assert evaluate("document.querySelector('.sandbox-badge').textContent==='Sandbox · test data'&&document.querySelector('.sandbox-caption').textContent==='Not booked'")
            for width in (320, 390, 768, 1440):
                call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 500})
                geometry = evaluate("""(()=>{const rows=[...document.querySelectorAll('.sandbox-transaction > summary')];
const rect=n=>({height:n.getBoundingClientRect().height, top:n.getBoundingClientRect().top});
return {rows:rows.map(rect), overflow:document.documentElement.scrollWidth>innerWidth,
clipped:[...document.querySelectorAll('.sandbox-money')].some(n=>n.scrollWidth>n.clientWidth+1),
controls:[...document.querySelectorAll('#sandbox-filters input,#sandbox-filters select,#sandbox-filters summary,#sandbox-filters button,#sandbox-filters a')].filter(n=>n.checkVisibility()).map(n=>({name:n.tagName, ...rect(n)}))};})()""")
                assert not geometry["overflow"], (width, geometry)
                assert not geometry["clipped"], (width, geometry)
                print(json.dumps({"width": width, "rows": geometry["rows"], "toolbar_rows": len({round(c["top"]) for c in geometry["controls"]})}))
                assert all(row["height"] >= 88 for row in geometry["rows"]), (width, geometry)
                assert all(control["height"] >= 48 for control in geometry["controls"]), (width, geometry)
                assert evaluate("document.querySelector('.sandbox-transaction > summary').getBoundingClientRect().top >= document.querySelector('#sandbox-filters').getBoundingClientRect().bottom"), (width, "rows follow the task toolbar")
                assert evaluate("[...document.querySelectorAll('#sandbox-filters input,#sandbox-filters select,#sandbox-filters button')].filter(n=>n.checkVisibility()).every(n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})"), (width, "toolbar controls stay inside the viewport")
                if 360 <= width < 1100:
                    assert evaluate("(()=>{const r=document.querySelector('.sandbox-transaction > summary');return getComputedStyle(r.querySelector('.sandbox-account-label')).gridRowStart==='2'&&getComputedStyle(r.querySelector('.sandbox-row-meta')).gridRowStart==='2';})()"), (width, "identity and wrapped metadata share the second grid band")
                elif width >= 1100:
                    columns = evaluate("""(()=>{
const bounds=n=>{const r=n.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width}};
const cells=row=>Object.fromEntries(['sandbox-merchant','sandbox-account-label','sandbox-date','sandbox-state','sandbox-money','sandbox-chevron'].map(name=>[name,bounds(row.querySelector('.'+name))]));
const head=document.querySelector('.sandbox-column-head');
return {head:cells(head),headTracks:getComputedStyle(head).gridTemplateColumns,
rows:[...document.querySelectorAll('.sandbox-transaction > summary')].map(row=>({cells:cells(row),tracks:getComputedStyle(row).gridTemplateColumns,money:row.querySelector('.sandbox-money').textContent}))};})()""")
                    print(json.dumps({"width": width, "columns": columns}))
                    evidence = os.environ.get("BANKING_DESIGN_EVIDENCE")
                    if evidence:
                        directory = Path(evidence)
                        directory.mkdir(parents=True, exist_ok=True)
                        (directory / "legacy-desktop-columns.json").write_text(json.dumps(columns, indent=2))
                    for row in columns["rows"]:
                        for name, cell in row["cells"].items():
                            # Money is end-aligned; compare its end edge, not text start.
                            edge = "right" if name in {"sandbox-money", "sandbox-chevron"} else "left"
                            assert abs(cell[edge] - columns["head"][name][edge]) < 1, (width, name, edge, columns)
                evaluate("document.querySelectorAll('.sandbox-page details').forEach(d=>d.open=true)")
                assert evaluate("document.documentElement.scrollWidth<=innerWidth"), width
                assert evaluate("[...document.querySelectorAll('.sandbox-filter-grid input,.sandbox-filter-grid select')].every(n=>n.getBoundingClientRect().height>=44)"), width
                for theme in ("light", "dark"):
                    evaluate(f"document.documentElement.dataset.theme='{theme}';document.documentElement.dataset.bsTheme='{theme}'")
                    assert evaluate("""(()=>{const row=document.querySelector('.sandbox-transaction'), suggestion=row.querySelector('.nancy');
const probe=document.createElement('span');probe.style.color='var(--nancy)';document.body.append(probe);
const orange=getComputedStyle(probe).color;probe.remove();
const values=Object.fromEntries([...row.querySelectorAll('dt')].map(n=>[n.textContent,n.nextElementSibling.textContent]));
return suggestion.getClientRects().length>0 && getComputedStyle(suggestion).color===orange
&& suggestion.textContent.includes('Suggested by Plaid') && suggestion.textContent.toLowerCase().includes('grocer')
&& values['Provider description']==='Generated merchant 114'
&& values['Original bank description']==='  BANK / 0089\\n<img src="https://untrusted.invalid/original" onerror="alert(1)"> & exact  '
&& values['Merchant']==='Normalized grocer' && values['Plaid counterparty 1'].includes('Type: marketplace')
&& row.querySelectorAll('img,script,iframe,a').length===0;})()"""), (width, theme)
                evaluate("document.querySelectorAll('.sandbox-page details').forEach(d=>d.open=false)")
            call("Emulation.setDeviceMetricsOverride", {"width": 320, "height": 900, "deviceScaleFactor": 1, "mobile": True})
            evaluate("document.querySelector('.sandbox-transaction > summary .sandbox-merchant').textContent='Generated merchant with a deliberately very long complete description for source verification'; document.querySelector('.sandbox-transaction').open=true")
            assert evaluate("(()=>{const n=document.querySelector('.sandbox-transaction > summary .sandbox-merchant');return getComputedStyle(n).whiteSpace!=='nowrap' && n.scrollWidth<=n.clientWidth+1;})()")
            evaluate("document.querySelector('#sandbox-staged .sandbox-transaction').remove(); document.querySelector('#sandbox-staged').style.display='none'")
            # Accounts are not full-screen edit forms until the user opens Edit account.
            source_start = source.index("  function renderAccounts()")
            source_end = source.index("  function updateLinkAvailability()")
            evaluate("document.querySelector('.sandbox-activity').insertAdjacentHTML('beforeend','<div id=\"sandbox-accounts\"></div>')")
            evaluate("(()=>{" + row_code + source[source_start:source_end] + """
state = {members: [{id:'alpha', name:'Generated member Alpha'}], connections:[{id:'generated',owner_id:'alpha',status:'connected'}],
accounts:[{name:'Generated Checking',connection_id:'generated',account_id:'same-account',selected:false,ownership:'unassigned',transaction_count:12,pending_count:1}]};
renderAccounts(); })()""")
            assert evaluate("document.querySelector('#sandbox-accounts details').open===false && document.querySelector('#sandbox-accounts form')===null && document.querySelector('#sandbox-accounts [data-action=edit-account]').getClientRects().length>0")
            evaluate("document.querySelector('#sandbox-accounts [data-action=edit-account]').click()")
            assert evaluate("document.querySelector('#sandbox-accounts form').getClientRects().length>0 && document.activeElement.tagName==='SELECT'")
            assert evaluate("document.querySelector('#sandbox-accounts').innerText.includes('Excluded')"), "account inclusion stays visible without opening the edit form"
            with TestClient(create_app(tmp_path / "connections-candidate", port=8772), base_url="http://127.0.0.1:8772") as client:
                connection_html = client.get("/connections").text
            connection_html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>|<link\b[^>]*>", "", connection_html)
            connection_html = connection_html.replace("</head>", f"<style>{styles}</style></head>")
            call("Page.setDocumentContent", {"frameId": frame, "html": connection_html})
            assert evaluate("document.querySelector('.sandbox-connect-panel').open===false && document.querySelector('#sandbox-link-form').getClientRects().length===0")
            for width in (320, 390, 768, 1440):
                call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 500})
                evaluate("document.querySelectorAll('.sandbox-page details').forEach(d=>d.open=true)")
                assert evaluate("document.documentElement.scrollWidth<=innerWidth"), (width, "expanded Connections")
                assert evaluate("[...document.querySelectorAll('.sandbox-page input:not([type=checkbox]),.sandbox-page select,.sandbox-page summary,.sandbox-page button')].filter(n=>n.getClientRects().length).every(n=>n.getBoundingClientRect().height>=44)"), (width, "expanded Connections controls")
            # Exercise the actual shared control relocation, not a header-shaped stub.
            with TestClient(create_app(tmp_path / "shell-candidate", port=8772), base_url="http://127.0.0.1:8772") as client:
                pages = {route: client.get(route).text for route in ("/", "/activity", "/accounts", "/connections")}
            for route, page_html in pages.items():
                page_html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>|<link\b[^>]*>", "", page_html)
                page_html = page_html.replace("</head>", f"<style>{styles}</style></head>")
                call("Page.setDocumentContent", {"frameId": frame, "html": page_html})
                evaluate((STATIC / "theme.js").read_text())
                evaluate("document.querySelector('#sandbox-status').textContent=''")
                if route == "/activity":
                    evaluate("(()=>{" + row_code + """
state = {members:[{id:'alpha', name:'Generated Alpha'}], connections:[], accounts:[]};
document.querySelector('#sandbox-staged').replaceChildren(row({name:'Generated merchant', account_name:'Generated Checking',
owner_id:'alpha', date:'2026-01-03', state:'posted', selected:true, amount_cents:'-1234', currency:'CAD'}));
document.querySelector('#sandbox-result-count').textContent='1–1 of 1 transactions';
})()""")
                for theme in ("light", "dark"):
                    evaluate(f"document.documentElement.dataset.theme='{theme}';document.documentElement.dataset.bsTheme='{theme}'")
                    for width in (320, 390):
                        call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": True})
                        shell = evaluate("""(()=>{const rect=n=>{const r=n.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}};
const header=document.querySelector('.fn-header'), pageHead=document.querySelector('.fn-page-head'), label=header.querySelector('label[for="appearance"]'), select=header.querySelector('[data-theme-select]');
return {title:pageHead.querySelector('h1').textContent, header:rect(header), label:rect(label), select:rect(select),
caption:rect(pageHead.querySelector('.sandbox-caption')), badge:rect(pageHead.querySelector('.sandbox-badge')),
titleRect:rect(pageHead.querySelector('h1')), firstRow:document.querySelector('.sandbox-transaction > summary')?.getBoundingClientRect().top,
overflow:document.documentElement.scrollWidth>innerWidth, nav:[...document.querySelectorAll('.fn-nav > a')].map(rect),
labelText:label.textContent, labelFor:label.htmlFor, selectID:select.id};})()""")
                        print(json.dumps({"route": route, "theme": theme, "width": width, "shell": shell}))
                        assert not shell["overflow"], (route, width, theme, shell)
                        assert shell["labelText"] == "Appearance" and shell["labelFor"] == shell["selectID"]
                        assert shell["label"]["width"] > 1 and shell["label"]["height"] > 1, "Appearance has a persistent visible label"
                        assert shell["select"]["height"] >= 48 and shell["select"]["width"] >= 48
                        assert all(tab["height"] >= 48 and tab["width"] >= 44 for tab in shell["nav"])
                        assert shell["titleRect"]["top"] >= shell["header"]["bottom"], shell
                        assert shell["caption"]["left"] >= 16 and shell["badge"]["right"] <= width - 16, shell
                        if route == "/activity":
                            assert shell["firstRow"] >= shell["titleRect"]["bottom"], shell
                    evaluate("window.originalTabs=[...document.querySelectorAll('.fn-nav > a')]")
                    for width in (1023, 1024, 1440):
                        call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
                        nav = evaluate("""(()=>{const bar=document.querySelector('.fn-nav'), r=bar.getBoundingClientRect(),
tabs=[...bar.children], main=document.querySelector('.app-main'), header=document.querySelector('.fn-header');
return {count:tabs.length,same:tabs.every((n,i)=>n===window.originalTabs[i]), display:getComputedStyle(bar).display,
top:r.top,bottom:r.bottom,height:r.height,mainTop:main.getBoundingClientRect().top,headerBottom:header.getBoundingClientRect().bottom,position:getComputedStyle(header).position,
overflow:document.documentElement.scrollWidth>innerWidth,
active:tabs.filter(n=>n.getAttribute('aria-current')==='page').map(n=>n.getAttribute('href')),
tabs:tabs.map(n=>({height:n.getBoundingClientRect().height,width:n.getBoundingClientRect().width}))};})()""")
                        print(json.dumps({"route": route, "theme": theme, "width": width, "nav": nav}))
                        assert nav["count"] == 4 and nav["same"] and nav["display"] == "flex", nav
                        assert nav["active"] == [route] and not nav["overflow"], nav
                        assert all(tab["height"] >= 48 and tab["width"] >= 44 for tab in nav["tabs"]), nav
                        assert nav["position"] == "static" and nav["mainTop"] >= nav["headerBottom"] >= nav["bottom"], nav
    finally:
        process.terminate()
        process.wait(timeout=10)
