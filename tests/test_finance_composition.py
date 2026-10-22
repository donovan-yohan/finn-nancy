"""The real application mounts the machine finance contract behind API auth."""
from fastapi.testclient import TestClient


def test_canonical_app_mounts_finance_router_behind_bearer(app_env, monkeypatch):
    monkeypatch.setenv('API_TOKEN', 'generated-local-api-test')
    from app.config import get_settings
    get_settings.cache_clear()
    from app.web.app import create_app
    client = TestClient(create_app())
    response = client.get('/api/finance/categories')
    assert response.status_code == 401
    response = client.get('/api/finance/categories', headers={'Authorization': 'Bearer generated-local-api-test'})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    assert client.get('/api/finance/transactions', headers={'Authorization': 'Bearer generated-local-api-test'}).status_code == 200
    get_settings.cache_clear()
