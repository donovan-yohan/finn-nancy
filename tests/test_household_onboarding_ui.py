"""Standalone synthetic template gates; no application/database imports.

Can run directly while backend source/fixtures are owned by another worker:
    .venv/bin/python tests/test_household_onboarding_ui.py
"""
from pathlib import Path
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]


def render(**updates):
    overview = {
        'members': [], 'connections': [], 'accounts': [],
        'consent_version': 'synthetic-consent-v7',
        'consent_text': 'Authorize selected accounts for this household.',
        'network_allowed': False, 'read_only': False,
        'setup': {'state': 'disabled', 'ready': False,
                  'message': 'Bank connections are disabled. Ask the operator to enable access.'},
    }
    overview.update(updates.pop('overview', {}))
    env = Environment(loader=FileSystemLoader(ROOT / 'app/web/templates'),
                      autoescape=select_autoescape(['html']))
    env.globals['pending_action_count'] = lambda: 0
    request = Request({'type': 'http', 'method': 'GET', 'scheme': 'http',
                       'path': '/connections', 'headers': [],
                       'server': ('fixture.test', 80), 'query_string': b''})
    return env.get_template('banking_connections.html').render(
        request=request, overview=overview, pending_links=updates.pop('pending_links', []), **updates)


class HouseholdTemplateTests(unittest.TestCase):
    def test_empty_household_starts_with_local_name_not_banks(self):
        html = render()
        self.assertIn('Add yourself', html)
        self.assertIn('Add other household members next', html)
        self.assertIn('data-household-onboarding', html)
        self.assertIn('for="member-name"', html)
        self.assertNotIn('Sample A', html)
        self.assertNotIn('Sample B', html)
        self.assertNotIn('/static/banking.js', html)
        self.assertIn('/static/foundation/vendor/bootstrap.min.css', html)
        self.assertIn('data-theme-host', html)
        self.assertIn('capture-announcer', html)
        self.assertIn('/static/capture-outbox.js', html)


    def test_pending_member_cannot_blindly_start_another_link(self):
        members = [{'id': 1, 'name': 'Synthetic Avery', 'needs_name': False, 'consented': True}]
        html = render(overview={'members': members, 'network_allowed': True},
                      pending_links=[{'session_id': 'pending-synthetic', 'member_name': 'Synthetic Avery',
                                      'state': 'pending'}])
        self.assertIn('Check connection for Synthetic Avery', html)
        self.assertNotIn('data-action="link"', html)
        self.assertIn('Finish the connection above before adding another bank', html)

    def test_uncertain_member_cannot_blindly_start_another_link(self):
        html = render(overview={'members': [{'id': 1, 'name': 'Synthetic Avery',
                       'needs_name': False, 'consented': True}], 'network_allowed': True},
                      pending_links=[{'session_id': 'uncertain-synthetic', 'member_name': 'Synthetic Avery',
                                      'state': 'uncertain'}])
        self.assertNotIn('data-action="link"', html)
        self.assertNotIn('data-action="finish"', html)
        self.assertIn('Connection outcome uncertain', html)

    def test_unresolved_link_also_disables_repair_but_preserves_recovery(self):
        from html.parser import HTMLParser

        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.buttons = []

            def handle_starttag(self, tag, attrs):
                if tag == 'button':
                    self.buttons.append(dict(attrs))

        for state in ['pending', 'exchanging', 'uncertain']:
            with self.subTest(state=state):
                member = {'id': 1, 'name': 'Synthetic Avery', 'needs_name': False,
                          'is_active': True, 'consented': True}
                html = render(overview={'members': [member], 'network_allowed': True,
                    'connections': [{'id': 7, 'member_id': 1, 'state': 'needs_repair', 'accounts': []}]},
                    pending_links=[{'session_id': 'generated-session', 'member_id': 1,
                                    'member_name': member['name'], 'state': state}])
                parser = Buttons()
                parser.feed(html)
                repair = [b for b in parser.buttons if b.get('data-action') == 'repair']
                self.assertTrue(not repair or all('disabled' in b for b in repair), repair)
                self.assertNotIn('data-action="link"', html)
                withdraw = [b for b in parser.buttons if b.get('data-action') == 'withdraw']
                self.assertTrue(withdraw and all('disabled' not in b for b in withdraw))
                if state == 'pending':
                    check = [b for b in parser.buttons if b.get('data-action') == 'finish']
                    self.assertTrue(check and all('disabled' not in b for b in check))
                else:
                    self.assertIn('Do not connect again', html)

    def test_repair_remains_available_without_unresolved_link(self):
        from re import search
        html = render(overview={'members': [{'id': 1, 'name': 'Synthetic Avery',
            'needs_name': False, 'is_active': True, 'consented': True}], 'network_allowed': True,
            'connections': [{'id': 7, 'member_id': 1, 'state': 'needs_repair', 'accounts': []}]})
        repair = search(r'<button[^>]+data-action="repair"[^>]*>', html)
        assert repair is not None
        self.assertNotIn('disabled', repair.group(0))
        self.assertIn('data-action="link"', html)

    def test_legacy_member_is_named_without_changing_identity_or_exposing_fake_person(self):
        html = render(overview={'members': [{'id': 7, 'name': 'Sample A',
                       'needs_name': True, 'consented': True}]})
        self.assertIn('Name required', html)
        self.assertIn('data-member-id="7"', html)
        self.assertIn('data-form="member-rename"', html)
        self.assertNotIn('Sample A', html)
        self.assertNotIn('data-action="link"', html)


    def test_two_member_account_editor_uses_explicit_choices_and_escaped_data(self):
        members = [{'id': 1, 'name': 'Synthetic Avery', 'needs_name': False, 'consented': True},
                   {'id': 2, 'name': 'Synthetic Rowan', 'needs_name': False, 'consented': False}]
        account = {'id': 44, 'provider_account_id': 'fixture<&>', 'name': '<script>bad()</script>',
                   'currency': 'CAD', 'kind': 'savings', 'account_id': None, 'included': None,
                   'ownership': None, 'owner_member_id': None, 'role': None, 'revision': 0}
        html = render(overview={'members': members, 'network_allowed': True,
                      'connections': [{'id': 82, 'member_id': 1, 'state': 'active', 'accounts': [account]}]})
        self.assertIn('Connection 1 · Synthetic Avery', html)
        self.assertNotIn('Connection 82 ·', html)
        self.assertIn('Give consent for Synthetic Rowan', html)
        self.assertIn('Connect bank for Synthetic Avery', html)
        self.assertIn('data-consent-version="synthetic-consent-v7"', html)
        self.assertIn('Choose personal or shared', html)
        self.assertIn('value="" selected>Choose personal or shared', html)
        self.assertIn('value="" selected>Choose an account', html)
        self.assertIn('data-new-account hidden disabled', html)
        self.assertIn('data-draft-key="82:fixture&lt;&amp;&gt;"', html)
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;', html)
        self.assertNotIn('<script>bad()', html)
        self.assertIn('Main source · New transactions', html)
        self.assertIn('Matching source · Existing transactions only', html)
        self.assertIn('data-save disabled', html)

    def test_unavailable_historical_owner_is_explicit_not_blank_or_connector(self):
        account = {'id': 4, 'provider_account_id': 'fixture-provider', 'name': 'Synthetic account',
                   'currency': 'CAD', 'kind': 'savings', 'account_id': 12, 'included': True,
                   'ownership': 'personal', 'owner_member_id': 99, 'role': 'primary', 'revision': 2}
        html = render(overview={'members': [{'id': 3, 'name': 'Synthetic Avery', 'needs_name': False,
                       'consented': True, 'is_active': True}], 'accounts': [], 'connections': [
                       {'id': 8, 'member_id': 3, 'state': 'active', 'accounts': [account]}]})
        self.assertIn('Personal owner unavailable', html)

    def test_consent_cancel_is_explicit_and_returns_to_originating_add_bank(self):
        html = render(overview={'members': [{'id': 3, 'name': 'Synthetic Avery', 'needs_name': False,
                       'consented': False, 'is_active': True}], 'network_allowed': True})
        self.assertIn('data-action="cancel-consent"', html)
        self.assertIn('data-member-id="3"', html)

    def test_unmapped_review_inclusion_requires_explicit_include_or_exclude(self):
        account = {'id': 44, 'provider_account_id': 'fixture-provider', 'name': 'Synthetic savings',
                   'currency': 'CAD', 'kind': 'savings', 'account_id': None, 'included': None,
                   'ownership': None, 'owner_member_id': None, 'role': None, 'revision': 0}
        html = render(overview={'members': [{'id': 3, 'name': 'Synthetic Avery', 'needs_name': False,
                       'consented': True, 'is_active': True}], 'connections': [
                       {'id': 8, 'member_id': 3, 'state': 'active', 'accounts': [account]}]})
        self.assertIn('value="" selected>Choose include or exclude', html)
        self.assertIn('value="include"', html)
        self.assertIn('value="exclude"', html)

    def test_pending_consent_can_be_withdrawn_and_unauthorized_finish_is_disabled(self):
        member = {'id': 3, 'name': 'Synthetic Avery', 'needs_name': False, 'consented': True, 'is_active': True}
        pending = [{'session_id': 'fixture-pending', 'member_id': 3, 'member_name': member['name'], 'state': 'pending'}]
        html = render(overview={'members': [member], 'network_allowed': True}, pending_links=pending)
        self.assertIn('data-action="withdraw"', html)
        member['consented'] = False
        html = render(overview={'members': [member], 'network_allowed': True}, pending_links=pending)
        from re import search
        match = search(r'<button[^>]+data-action="finish"[^>]+>', html)
        assert match is not None
        self.assertIn('disabled', match.group(0))
        self.assertIn('data-link-allowed="false"', html)

    def test_inactive_rename_recovery_does_not_enable_banks_and_corrected_connections_remain(self):
        html = render(overview={'members': [{'id': 9, 'name': 'Name required', 'needs_name': True,
                       'consented': True, 'is_active': False}], 'network_allowed': True})
        self.assertNotIn('data-action="link"', html)
        self.assertIn('data-form="member-rename"', html)
        html = render(overview={'members': [], 'network_allowed': True,
                      'connections': [{'id': 88, 'member_id': 9, 'member_name': 'Synthetic corrected',
                                       'state': 'active', 'accounts': []}]})
        self.assertIn('Connection 1 · Synthetic corrected', html)
        self.assertIn('Inactive member', html)
        self.assertNotIn('data-action="link"', html)
        self.assertNotIn('data-action="accounts"', html)

    def test_removal_uncertainty_blocks_new_link_and_explains_existing_connection(self):
        html = render(overview={'members': [{'id': 1, 'name': 'Synthetic Avery',
                      'needs_name': False, 'consented': True}], 'network_allowed': True,
                      'connections': [{'id': 3, 'member_id': 1, 'state': 'remove_uncertain', 'accounts': []}]})
        self.assertNotIn('data-action="link"', html)
        self.assertIn('Disconnection is not confirmed', html)
        self.assertIn('Check access in Plaid or your bank', html)

    def test_saved_account_summary_has_economic_owner_separate_from_connector(self):
        account = {'id': 44, 'provider_account_id': 'fixture-provider', 'name': 'Synthetic savings',
                   'currency': 'CAD', 'kind': 'savings', 'account_id': 12, 'included': True,
                   'ownership': 'personal', 'owner_member_id': 2, 'role': 'secondary', 'revision': 3}
        html = render(overview={'members': [
            {'id': 1, 'name': 'Synthetic Avery', 'needs_name': False, 'consented': True},
            {'id': 2, 'name': 'Synthetic Rowan', 'needs_name': False, 'consented': True}],
            'network_allowed': True, 'accounts': [{'id': 12, 'name': 'Ledger savings', 'currency': 'CAD'}],
            'connections': [{'id': 3, 'member_id': 1, 'state': 'active', 'accounts': [account]}]})
        from re import search
        match = search(r'<p class="household-account-summary">(.*?)</p>', html, flags=16)
        assert match is not None
        summary = match.group(1)
        self.assertIn('Synthetic Rowan', summary)
        self.assertNotIn('Synthetic Avery', summary)
        self.assertIn('Ledger savings', summary)
        self.assertIn('Included in review', summary)
        self.assertIn('Matching source', summary)

    def test_read_only_disables_all_mutations_but_navigation_survives(self):
        from html.parser import HTMLParser

        class Controls(HTMLParser):
            controls = []

            def handle_starttag(self, tag, attrs):
                if tag in ('input', 'button', 'select'):
                    self.controls.append(dict(attrs))

        html = render(overview={'read_only': True, 'members': [
            {'id': 1, 'name': 'Synthetic Avery', 'needs_name': False, 'consented': True}]})
        parser = Controls()
        parser.feed(html)
        for control in parser.controls:
            if control.get('id') == 'appearance' or control.get('data-action') in ('refresh', 'cancel-consent'):
                continue
            self.assertIn('disabled', control)
        self.assertIn('href="/bank-feed"', html)
        self.assertIn('More navigation', html)
        self.assertIn('href="/upload"', html)


def test_pinned_design_foundation_snapshot_is_unchanged():
    import hashlib
    import json

    foundation = ROOT / 'app/web/static/foundation'
    snapshot = json.loads((foundation / 'SOURCE.json').read_text())
    assert snapshot['bootstrap'] == '5.3.8'
    for relative, digest in snapshot['sha256'].items():
        assert hashlib.sha256((foundation / relative).read_bytes()).hexdigest() == digest


def test_real_backend_router_renders_empty_then_named_household_without_provider(empty_db, monkeypatch):
    """Vertical application template/API proof; no moving fixture assumptions."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.banking.service import BankingService
    from app.banking.web import build_router
    from app.banking.client import PlaidClient
    from app.config import Settings, get_settings

    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    get_settings.cache_clear()

    def forbidden(*args, **kwargs):
        raise AssertionError('Local household onboarding must not call Plaid')

    monkeypatch.setattr(PlaidClient, 'post', forbidden)
    service = BankingService(db_path=empty_db, private_dir=None, credentials_file=None,
        origin='http://banking.example.invalid', completion_uri='http://banking.example.invalid/connections',
        enabled=lambda: False, strict_local=lambda: True, read_only=lambda: False)
    app = FastAPI()
    app.include_router(build_router(service))
    with TestClient(app, base_url='http://banking.example.invalid') as client:
        page = client.get('/connections')
        assert page.status_code == 200
        assert 'Add yourself' in page.text
        assert 'Sample Member' not in page.text
        csrf = client.get('/connections/data/overview').json()['csrf']
        headers = {'Origin': 'http://banking.example.invalid', 'X-Banking-CSRF': csrf}
        ids = []
        for name in ['Synthetic Avery', 'Synthetic Rowan']:
            response = client.post('/connections/data/member', json={'name': name}, headers=headers)
            assert response.status_code == 200
            ids.append(response.json()['member_id'])
        assert len(set(ids)) == 2
        page = client.get('/connections')
        assert 'Add family member' in page.text
        for id_, name in zip(ids, ['Synthetic Avery', 'Synthetic Rowan']):
            assert f'data-member="{id_}"' in page.text
            assert f'Add bank for {name}' in page.text
            assert f'No banks connected for {name}' in page.text
        response = client.post(f'/connections/data/member/{ids[0]}',
                               json={'name': 'Éloïse <&>'}, headers=headers)
        assert response.status_code == 200
        page = client.get('/connections')
        assert 'Éloïse &lt;&amp;&gt;' in page.text
        assert 'Synthetic Avery' not in page.text
        assert client.get('/connections/data/overview').json()['setup']['state'] == 'disabled'
    get_settings.cache_clear()


if __name__ == '__main__':
    unittest.main()
