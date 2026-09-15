#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8799}"
TOKEN="${API_TOKEN:-}"
INPUT="${1:-}"

if [[ -z "$INPUT" || ! -f "$INPUT" ]]; then
  echo "usage: API_TOKEN=... BASE_URL=... $0 /path/to/receipt-or-statement" >&2
  exit 2
fi

if [[ -z "$TOKEN" ]]; then
  echo "API_TOKEN is required" >&2
  exit 2
fi

json_field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"
}

echo "POST /api/ingest"
ingest_json="$(
  curl -fsS \
    -H "Authorization: Bearer $TOKEN" \
    -F "file=@${INPUT}" \
    "$BASE_URL/api/ingest"
)"
echo "$ingest_json"
doc_id="$(printf '%s' "$ingest_json" | json_field doc_id)"
status_url="$(printf '%s' "$ingest_json" | json_field status_url)"

if [[ -z "$doc_id" || -z "$status_url" ]]; then
  echo "ingest response missing doc_id/status_url" >&2
  exit 1
fi

echo "Poll $status_url"
deadline=$((SECONDS + 60))
status=""
while (( SECONDS < deadline )); do
  status_json="$(curl -fsS -H "Authorization: Bearer $TOKEN" "$status_url")"
  echo "$status_json"
  status="$(printf '%s' "$status_json" | json_field status)"
  # Terminal document statuses: processed (receipt filed / statement staged),
  # matched (statement fully reconciled), needs_review (parked for a human).
  if [[ "$status" == "processed" || "$status" == "matched" || "$status" == "needs_review" ]]; then
    break
  fi
  sleep 2
done

if [[ "$status" != "processed" && "$status" != "matched" && "$status" != "needs_review" ]]; then
  echo "timed out waiting for doc $doc_id to process; last status: $status" >&2
  exit 1
fi

sample_body='{"amount_cents":-1234,"posted_on":"2026-07-09","description":"API acceptance sample","counterparty":"Acceptance","category":"Restaurants","source":"api_acceptance","external_id":"acceptance-sample-1"}'

echo "POST /api/transactions first"
first_tx="$(
  curl -fsS \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$sample_body" \
    "$BASE_URL/api/transactions"
)"
echo "$first_tx"

echo "POST /api/transactions repeat"
second_tx="$(
  curl -fsS \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$sample_body" \
    "$BASE_URL/api/transactions"
)"
echo "$second_tx"

echo "GET /api/reconcile without token expects 401"
tmp_body="$(mktemp)"
code="$(curl -sS -o "$tmp_body" -w '%{http_code}' "$BASE_URL/api/reconcile" || true)"
cat "$tmp_body"
rm -f "$tmp_body"
echo
if [[ "$code" != "401" ]]; then
  echo "expected 401 without token, got $code" >&2
  exit 1
fi

echo "GET /api/reconcile with token"
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/reconcile"
echo
