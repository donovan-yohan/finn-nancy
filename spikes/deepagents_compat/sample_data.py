from __future__ import annotations

SAMPLE_TRANSACTIONS: list[dict[str, object]] = [
    {
        "id": "txn-001",
        "posted_date": "2026-07-02",
        "account": "checking",
        "merchant": "Green Grocer",
        "category": "groceries",
        "amount_cents": -4230,
    },
    {
        "id": "txn-002",
        "posted_date": "2026-07-03",
        "account": "checking",
        "merchant": "Metro Pass",
        "category": "transit",
        "amount_cents": -3250,
    },
    {
        "id": "txn-003",
        "posted_date": "2026-07-05",
        "account": "checking",
        "merchant": "Payroll",
        "category": "income",
        "amount_cents": 250000,
    },
]


def spend_total(account: str, month: str) -> int:
    prefix = f"{month}-"
    return sum(
        int(txn["amount_cents"])
        for txn in SAMPLE_TRANSACTIONS
        if txn["account"] == account
        and str(txn["posted_date"]).startswith(prefix)
        and int(txn["amount_cents"]) < 0
    )


def candidate_match(amount_cents: int, merchant_hint: str | None = None) -> list[dict[str, object]]:
    hint = (merchant_hint or "").lower()
    matches: list[dict[str, object]] = []
    for txn in SAMPLE_TRANSACTIONS:
        merchant = str(txn["merchant"])
        if int(txn["amount_cents"]) == amount_cents or (hint and hint in merchant.lower()):
            matches.append(
                {
                    "transaction_id": txn["id"],
                    "merchant": merchant,
                    "amount_cents": txn["amount_cents"],
                    "confidence": 0.97 if int(txn["amount_cents"]) == amount_cents else 0.72,
                }
            )
    return matches
