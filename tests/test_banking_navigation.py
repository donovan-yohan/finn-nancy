"""Shared navigation must expose the daily bank review workflow."""
from fastapi.testclient import TestClient


def test_bank_feed_is_reachable_from_shared_navigation(app_env):
    from app.web.app import create_app
    client = TestClient(create_app())
    for route in ('/', '/activity', '/connections', '/bank-feed'):
        response = client.get(route)
        assert response.status_code == 200
        nav = response.text.split('<nav class="tabbar"', 1)[1]
        assert 'href="/bank-feed"' in nav
        assert 'href="/connections"' in nav
