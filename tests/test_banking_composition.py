"""Canonical production composition remains offline unless explicitly enabled."""
from fastapi.testclient import TestClient


def test_app_owns_one_banking_router_without_constructing_secret_store(app_env, monkeypatch):
    monkeypatch.setenv('PLAID_ENABLED', 'false')
    monkeypatch.delenv('PLAID_PRIVATE_DIR', raising=False)
    monkeypatch.delenv('PLAID_CREDENTIALS_FILE', raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.web.app import create_app
    application = create_app()
    paths = application.openapi()['paths']
    assert '/connections' in paths
    assert '/bank-feed' in paths
    assert '/connections/data/overview' in paths
    service = application.state.banking_service
    assert service.private_dir is None
    assert service.credentials_file is None
    assert service._store is None
    client = TestClient(application)
    response = client.get('/connections/data/overview')
    assert response.status_code == 200, response.text
    assert response.json()['network_allowed'] is False
    assert service._store is None
    assert client.post('/connections/data/member', json={'name': 'Generated member'}).status_code == 403
    get_settings.cache_clear()
