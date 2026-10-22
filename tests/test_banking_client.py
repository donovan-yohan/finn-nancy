"""Generated banking transport fixtures; never dispatch to a real provider."""
import json
import secrets

import httpx
import pytest


def test_private_production_transport_checks_policy_each_dispatch(tmp_path):
    from app.banking.client import PlaidClient, BankingError
    from app.banking.secrets import PrivateStore

    root = tmp_path / 'private'
    store = PrivateStore(root)
    ref = store.put('access-' + secrets.token_hex(16))
    assert store.get(ref).startswith('access-')
    assert (root / ref).stat().st_mode & 0o777 == 0o600
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'client_id': secrets.token_hex(12), 'secret': secrets.token_hex(24)}))
    credentials.chmod(0o600)
    allowed = [True]
    requests = []
    def policy():
        if not allowed[0]:
            raise BankingError('egress_blocked')
    def respond(request):
        requests.append(request)
        assert str(request.url) == 'https://production.plaid.com/accounts/get'
        return httpx.Response(200, content=b'{"amount":12.34}')
    client = PlaidClient(credentials, policy=policy, transport=httpx.MockTransport(respond))
    from decimal import Decimal
    assert client.post('/accounts/get', {'access_token': store.get(ref)})['amount'] == Decimal('12.34')
    allowed[0] = False
    with pytest.raises(BankingError, match='egress_blocked'):
        client.post('/accounts/get', {})
    assert len(requests) == 1
    (root / ref).chmod(0o644)
    with pytest.raises(BankingError, match='private'):
        store.get(ref)


@pytest.mark.parametrize('status,body,expected',[
    (302,b'{}','provider_request_failed'),
    (400,b'{"error_code":"UNSAFE_SECRET_generated","error_message":"generated-secret"}','provider_request_failed'),
    (200,b'{"amount":NaN}','provider_request_uncertain'),
    (200,b'[]','invalid_provider_response'),
    (200,b' '+b'x'*2_000_001,'provider_response_too_large'),
])
def test_provider_errors_are_bounded_sanitized_and_never_follow_redirects(tmp_path,status,body,expected):
    from app.banking.client import PlaidClient,BankingError
    credentials=tmp_path/'generated.json'
    credentials.write_text(json.dumps({'client_id':secrets.token_hex(12),'secret':secrets.token_hex(24)}))
    credentials.chmod(0o600)
    calls=[]
    def response(req):
        calls.append(req.url)
        return httpx.Response(status,content=body,headers={'Location':'https://evil.example.invalid'})
    client=PlaidClient(credentials,policy=lambda:None,transport=httpx.MockTransport(response))
    with pytest.raises(BankingError,match=expected):
        client.post('/accounts/get',{})
    assert len(calls)==1


def test_private_store_refuses_symlink_and_loose_directory(tmp_path):
    from app.banking.secrets import PrivateStore
    root=tmp_path/'private';store=PrivateStore(root)
    target=tmp_path/'generated-target';target.write_text('generated');target.chmod(0o600)
    ref='a'*48;(root/ref).symlink_to(target)
    with pytest.raises(ValueError,match='private'):
        store.get(ref)
    root.chmod(0o755)
    with pytest.raises(ValueError,match='private'):
        store.put('generated')
