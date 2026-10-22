"""Generated Hosted Link responses use the documented token/get response shape."""
import json
import secrets

import httpx
import pytest

from app.db import engine


def test_hosted_link_server_consumes_its_own_token_once(empty_db, tmp_path):
    from app.banking.service import BankingService, CONSENT_VERSION
    credential = tmp_path / 'credentials.json'
    credential.write_text(json.dumps({'client_id': secrets.token_hex(12), 'secret': secrets.token_hex(24)}))
    credential.chmod(0o600)
    link = 'generated-link-' + secrets.token_hex(12)
    public = 'generated-public-' + secrets.token_hex(12)
    access = 'generated-access-' + secrets.token_hex(12)
    calls = []
    def provider(req):
        body = json.loads(req.content)
        calls.append(req.url.path)
        if req.url.path == '/link/token/create':
            assert body['hosted_link']['completion_redirect_uri'] == 'http://banking.example.invalid/connections'
            assert body['user']['client_user_id'].endswith(':member:1')
            assert 'webhook' not in body
            return httpx.Response(200,json={'link_token':link,'hosted_link_url':'https://secure.plaid.com/hl/generated','expiration':'2099-01-01T00:00:00Z'})
        if req.url.path == '/link/token/get':
            assert body['link_token']==link
            return httpx.Response(200,json={'link_token':link,'link_sessions':[{'link_session_id':'generated-session',
                'finished_at':'2026-06-01T00:00:00Z','results':{'item_add_results':[{'public_token':public}]},
                'on_success':{'public_token':public,'metadata':{}}}]})
        if req.url.path == '/item/public_token/exchange':
            assert body['public_token']==public
            return httpx.Response(200,json={'access_token':access,'item_id':'generated-item'})
        if req.url.path == '/accounts/get':
            assert body['access_token']==access
            return httpx.Response(200,json={'accounts':[{'account_id':'generated-account','name':'Checking',
                'type':'depository','subtype':'checking','balances':{'iso_currency_code':'CAD'}}]})
        raise AssertionError(req.url.path)
    service = BankingService(db_path=empty_db, private_dir=tmp_path/'private', credentials_file=credential,
        origin='http://banking.example.invalid',completion_uri='http://banking.example.invalid/connections', enabled=lambda:True,
        strict_local=lambda:False,read_only=lambda:False,transport=httpx.MockTransport(provider))
    service.consent(1, granted=True,version=CONSENT_VERSION)
    started = service.start_link(member_id=1,browser='generated-browser')
    assert set(started)=={'session_id','hosted_link_url'}
    assert service.browser_links('generated-browser')[0]['session_id']==started['session_id']
    assert service.browser_links('another-browser')==[]
    with pytest.raises(ValueError,match='browser_session_mismatch'):
        service.finish_link(started['session_id'],browser='another-browser')
    result = service.finish_link(started['session_id'],browser='generated-browser')
    assert result['status']=='connected'
    assert service.finish_link(started['session_id'],browser='generated-browser')==result
    assert calls.count('/item/public_token/exchange')==1
    serialized = json.dumps(service.overview())
    assert all(secret not in serialized for secret in (link,public,access))
    with engine.read_conn(empty_db) as conn:
        dump='\n'.join(conn.iterdump())
        assert all(secret not in dump for secret in (link,public,access))
        assert conn.execute('SELECT COUNT(*) FROM bank_accounts').fetchone()[0]==1
    service.consent(1,granted=False,version=CONSENT_VERSION)
    count=len(calls)
    with pytest.raises(ValueError,match='current_member_consent_required'):
        service.disconnect(result['connection_id'])
    assert len(calls)==count
    service.consent(1,granted=True,version=CONSENT_VERSION)
    def fail_remove(req):
        calls.append(req.url.path)
        raise httpx.ReadTimeout('generated uncertain response')
    service.transport=httpx.MockTransport(fail_remove)
    with pytest.raises(ValueError,match='revocation_uncertain_do_not_retry'):
        service.disconnect(result['connection_id'])
    with pytest.raises(ValueError,match='revocation_uncertain_do_not_retry'):
        service.disconnect(result['connection_id'])
    assert calls.count('/item/remove')==1


def test_uncertain_exchange_is_never_retried(empty_db,tmp_path):
    from app.banking.service import BankingService,CONSENT_VERSION
    credentials=tmp_path/'generated.json'
    credentials.write_text(json.dumps({'client_id':secrets.token_hex(12),'secret':secrets.token_hex(24)}));credentials.chmod(0o600)
    calls=[]
    def provider(req):
        calls.append(req.url.path)
        if req.url.path=='/link/token/create':
            return httpx.Response(200,json={'link_token':'generated-link','hosted_link_url':'https://secure.plaid.com/hl/generated','expiration':'2099-01-01T00:00:00Z'})
        if req.url.path=='/link/token/get':
            return httpx.Response(200,json={'link_token':'generated-link','link_sessions':[{'finished_at':'generated-finished','on_success':{'public_token':'generated-public'}}]})
        raise httpx.ReadTimeout('generated uncertain response')
    service=BankingService(db_path=empty_db,private_dir=tmp_path/'private',credentials_file=credentials,
        origin='http://banking.example.invalid',completion_uri='http://banking.example.invalid/connections',
        enabled=lambda:True,strict_local=lambda:False,read_only=lambda:False,transport=httpx.MockTransport(provider))
    service.consent(1,granted=True,version=CONSENT_VERSION)
    started=service.start_link(member_id=1,browser='generated-browser')
    for _ in range(2):
        with pytest.raises(ValueError,match='link_outcome_uncertain_do_not_retry'):
            service.finish_link(started['session_id'],browser='generated-browser')
    assert calls.count('/item/public_token/exchange')==1
