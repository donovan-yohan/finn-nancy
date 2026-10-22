"""Remote CLI uses HTTP only, never the operator's local ledger or dotenv."""
from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from app.cli import app


def test_remote_command_sends_json_and_reports_correlation(monkeypatch):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={'rows': [{'id': 7, 'amount_cents': -1234}]},
            headers={'X-Request-ID': 'generated-request', 'X-FN-Caller': 'api'})

    transport = httpx.MockTransport(provider)
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: transport)
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    result = CliRunner().invoke(app, ['api', 'GET', '/api/finance/transactions?limit=1'])
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert value == {'status_code': 200, 'request_id': 'generated-request', 'caller': 'api',
                     'data': {'rows': [{'id': 7, 'amount_cents': -1234}]}}
    assert len(calls) == 1
    assert calls[0].headers['authorization'] == 'Bearer generated-remote-key'
    assert calls[0].url == 'https://finance.example.invalid/api/finance/transactions?limit=1'
    assert 'generated-remote-key' not in result.output


def test_remote_form_stdin_uses_same_frontend_endpoint_without_followup(monkeypatch):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(303, headers={'Location': '/actions'})

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(provider))
    monkeypatch.setenv('FN_API_URL', 'http://127.0.0.1:8771')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    result = CliRunner().invoke(app, ['api', 'POST', '/actions/9/approve', '--data', '-', '--form'],
        input=json.dumps({'to_category_id': 4, 'override': '0'}))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['status_code'] == 303
    assert len(calls) == 1
    assert calls[0].content == b'to_category_id=4&override=0'
    assert calls[0].headers['content-type'].startswith('application/x-www-form-urlencoded')


def test_remote_json_file_preserves_explicit_types(monkeypatch, tmp_path):
    payload = {'name': 'Generated member', 'included': False, 'account_id': None}
    path = tmp_path / 'generated-request.json'
    path.write_text(json.dumps(payload))
    calls = []
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={'saved': True})))
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    result = CliRunner().invoke(app, ['api', 'POST', '/connections/data/member', '--data', str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(calls[0].content) == payload


def test_remote_missing_key_does_not_load_application_settings(monkeypatch):
    monkeypatch.delenv('FN_API_KEY', raising=False)
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setattr('app.cli.get_settings', lambda: pytest.fail('remote CLI must not load local settings'))
    result = CliRunner().invoke(app, ['api', 'GET', '/api/finance/categories'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['error'] == 'FN_API_KEY is required'


def test_remote_errors_are_structured_and_do_not_echo_transport_secrets(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')

    def provider(request):
        raise httpx.ReadTimeout('generated-remote-key private request body')

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(provider))
    result = CliRunner().invoke(app, ['api', 'POST', '/actions/9/approve'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['error'] == 'request outcome uncertain; inspect current state before retrying'
    assert 'generated-remote-key' not in result.output
    assert 'private request body' not in result.output


def test_remote_http_failure_returns_status_and_nonzero_exit(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(409, json={'detail': 'stale_review_plan'})))
    result = CliRunner().invoke(app, ['api', 'POST', '/connections/data/decision/7'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['data'] == {'detail': 'stale_review_plan'}
    assert json.loads(result.stdout)['status_code'] == 409


@pytest.mark.parametrize('base,path', [
    ('https://' + 'user:password@' + 'example.invalid', '/'),
    ('https://finance.example.invalid?token=forbidden', '/'),
    ('https://finance.example.invalid', 'https://other.example.invalid/'),
    ('https://finance.example.invalid', '//other.example.invalid/'),
    ('https://finance.example.invalid', '/foo#fragment'),
])
def test_remote_rejects_credential_or_cross_authority_urls(monkeypatch, base, path):
    monkeypatch.setenv('FN_API_URL', base)
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: pytest.fail('must reject before transport'))
    result = CliRunner().invoke(app, ['api', 'GET', path])
    assert result.exit_code == 1
    assert 'error' in json.loads(result.stdout)
    assert 'password' not in result.output


def test_remote_never_follows_an_external_redirect_or_proxy(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.invalid:8888')
    calls = []
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(302, headers={'Location': 'https://other.example.invalid/'})))
    result = CliRunner().invoke(app, ['api', 'GET', '/activity'])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0].url.host == 'finance.example.invalid'


def test_remote_response_is_bounded(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: httpx.Response(200, content=b'x' * (4_000_000 + 1))))
    result = CliRunner().invoke(app, ['api', 'GET', '/activity'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['error'] == 'response exceeds 4000000 bytes'


@pytest.mark.parametrize('payload', ['null', '[]', '{"value":NaN}'])
def test_remote_rejects_nonobject_or_nonfinite_request_without_dispatch(monkeypatch, payload):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: pytest.fail('invalid JSON must not dispatch'))
    result = CliRunner().invoke(app, ['api', 'POST', '/connections/data/member', '--data', '-'], input=payload)
    assert result.exit_code == 1
    assert 'error' in json.loads(result.stdout)


def test_remote_rejects_invalid_port_without_echoing_connection_input(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid:generated-secret')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    result = CliRunner().invoke(app, ['api', 'GET', '/activity'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['error'] == 'invalid connection configuration'
    assert 'generated-secret' not in result.output


def test_remote_stdin_limit_counts_utf8_bytes_before_dispatch(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    calls = []
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={'saved': True})))
    payload = json.dumps({'name': '界' * 400_000}, ensure_ascii=False).encode('utf-8')
    assert len(payload) > 1_000_000
    result = CliRunner().invoke(app, ['api', 'POST', '/connections/data/member', '--data', '-'], input=payload)
    assert result.exit_code == 1
    assert json.loads(result.stdout)['error'] == 'request data exceeds 1000000 bytes'
    assert calls == []


@pytest.mark.parametrize('suffix', ['?', '#'])
def test_remote_rejects_empty_origin_delimiters_before_dispatch(monkeypatch, suffix):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid' + suffix)
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    calls = []
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={'saved': True})))
    result = CliRunner().invoke(app, ['api', 'POST', '/generated'])
    assert result.exit_code == 1
    assert 'error' in json.loads(result.stdout)
    assert calls == []


@pytest.mark.parametrize('body', ['{"value":NaN}', '{"value":1e999}'])
def test_remote_nonfinite_response_keeps_status_and_correlation(monkeypatch, body):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    calls = []
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, text=body,
            headers={'X-Request-ID': 'generated-request', 'X-FN-Caller': 'api'})))
    result = CliRunner().invoke(app, ['api', 'POST', '/generated'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {'status_code': 200, 'request_id': 'generated-request',
                                       'caller': 'api', 'data': body}
    assert len(calls) == 1


def test_remote_final_url_invalidity_is_structured_before_dispatch(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-remote-key')
    calls = []
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)))
    result = CliRunner().invoke(app, ['api', 'POST', '/' + 'x' * 70_000])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['error'] == 'invalid connection configuration'
    assert calls == []


def test_remote_nonascii_key_is_rejected_with_sanitized_message(monkeypatch):
    monkeypatch.setenv('FN_API_URL', 'https://finance.example.invalid')
    monkeypatch.setenv('FN_API_KEY', 'generated-界-key')
    calls = []
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)))
    result = CliRunner().invoke(app, ['api', 'POST', '/generated'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['error'] == 'invalid connection configuration'
    assert calls == []
