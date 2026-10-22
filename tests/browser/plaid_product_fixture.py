"""Generated-only product browser fixtures; never reads live settings or data."""
from pathlib import Path

import httpx

from app.plaid_sandbox.client import SandboxError
from app.plaid_sandbox.web import create_app
from tests.test_plaid_sandbox_service import GeneratedPlaid, connected, page, row


def product_row(transaction_id, amount="12.34", **fields):
    return {**row(transaction_id, **fields), "amount": amount}


def generated_product_app(root: Path, port: int):
    provider = GeneratedPlaid()
    app = create_app(root, port=port, transport=httpx.MockTransport(provider))
    store, service = app.state.store, app.state.service
    store.configure("generated-client", "generated-secret", "plaid-sandbox.v1")
    alpha, owner = connected(service, store, "Generated member Alpha", "alpha")
    # A selected account with posted, pending and unknown-currency rows.
    provider.pages = [page([
        product_row(f"posted-{index:03}", amount="12.34", name=f"Generated merchant {index:03}", **({
            "merchant_name": "Generated Northstar Market",
            "original_description": "CARD 0042 GENERATED NORTHSTAR MARKET",
            "merchant_entity_id": "generated-merchant-entity",
            "logo_url": "https://generated-logos.invalid/never-fetch.png",
            "website": "generated-market.invalid",
            "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES",
                                          "confidence_level": "HIGH", "version": "v2"},
            "counterparties": [{"name": "Generated Northstar Market", "type": "merchant",
                                "entity_id": "generated-merchant-entity", "confidence_level": "HIGH"},
                               {"name": "Generated Delivery", "type": "marketplace",
                                "entity_id": "generated-marketplace", "confidence_level": "VERY_HIGH"}],
        } if index == 1 else {}))
        for index in range(115)
    ] + [
        product_row("pending-only", amount="0.01", pending=True, name="Generated pending merchant"),
        product_row("unknown-currency", amount="-20.01", iso_currency_code=None, name="Generated unknown currency"),
        product_row("large-cents", amount="89999999999999.99", name="Generated precise cents"),
    ])]
    service.sync(alpha, "generated-fixture")
    beta, beta_owner = connected(service, store, "Generated member Beta", "beta")
    store.map_account(beta, "same-account-id", selected=False, ownership="unassigned", actor="generated-fixture")
    provider.pages = [page([product_row("excluded", amount="-20.01", iso_currency_code="USD", name="Generated excluded merchant")])]
    service.sync(beta, "generated-fixture")
    # Missing bank-provided refresh time stays unknown, independent of local sync.
    store.commit_window(beta, store.connection(beta)["cursor"], store.connection(beta)["cursor"], [], None, "generated-fixture")
    # Retained historical evidence with explicit removed/superseded states.
    provider.pages = [page([product_row("pending-before", pending=True, name="Generated prior pending")])]
    service.sync(alpha, "generated-fixture")
    provider.pages = [page([product_row("replacement", pending_transaction_id="pending-before", name="Generated replacement")], removed=[{"transaction_id": "pending-before"}, {"transaction_id": "posted-000"}])]
    service.sync(alpha, "generated-fixture")
    # An actual generated transport timeout preserves an unresolved exchange.
    attempt = service.begin("generated-fixture", beta_owner, consent="plaid-sandbox.v1")
    provider.fail_exchange = True
    try:
        service.exchange("generated-fixture", attempt["session_id"], provider.completions[attempt["link_token"]])
    except SandboxError as exc:
        assert str(exc) == "exchange_uncertain_do_not_replay"
    else:
        raise AssertionError("generated uncertain exchange did not fail closed")
    provider.fail_exchange = False
    # Subsequent person-driven sync is deterministic and does not fabricate new rows.
    provider.pages = [page()] * 20
    return app
