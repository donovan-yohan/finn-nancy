"""Generated canonical bank-feed evidence actions resolve real app routes."""
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.test_banking_feed import banking  # noqa: F401


def test_associated_feed_links_to_canonical_transaction_editor(banking, monkeypatch):
    service, connection, account, pages, calls = banking
    monkeypatch.setenv('DB_PATH', str(service.db_path))
    get_settings.cache_clear()
    service.sync(connection)
    subject = service.feed()[0]
    plan = service.review_plan(subject['id'])
    booked = service.decide(subject['id'], fingerprint=plan['fingerprint'], operation_key='link:book',
        action='new', flow_kind='purchase', reason='Generated reviewed expense')
    tid = booked['transaction_id']
    from app.web.app import create_app
    client = TestClient(create_app())
    response = client.get('/bank-feed')
    assert response.status_code == 200
    target = f'/txn/{tid}/edit'
    assert f'href="{target}"' in response.text
    assert client.get(target).status_code == 200
    get_settings.cache_clear()
