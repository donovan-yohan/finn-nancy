"""Shared navigation must expose the daily bank review workflow."""
from html.parser import HTMLParser

from fastapi.testclient import TestClient


class NavigationLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag == 'nav':
            self.depth += 1
        if tag == 'a' and self.depth:
            self.links.add(dict(attrs).get('href'))

    def handle_endtag(self, tag):
        if tag == 'nav':
            self.depth -= 1


def test_bank_feed_is_reachable_from_shared_navigation(app_env):
    from app.web.app import create_app
    client = TestClient(create_app())
    for route in ('/', '/activity', '/connections', '/bank-feed'):
        response = client.get(route)
        assert response.status_code == 200
        navigation = NavigationLinks()
        navigation.feed(response.text)
        assert '/bank-feed' in navigation.links
        assert '/connections' in navigation.links
