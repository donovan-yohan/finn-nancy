"""Browser boundary uses exact trusted origin, HttpOnly session and CSRF."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.banking.service import BankingService


def test_disabled_pages_are_read_only_and_browser_commands_require_origin_session(empty_db,tmp_path):
    from app.banking.web import build_router
    service=BankingService(db_path=empty_db, private_dir=None,credentials_file=None,
        origin='http://banking.example.invalid',completion_uri='http://banking.example.invalid/connections',
        enabled=lambda:False,strict_local=lambda:True,read_only=lambda:False)
    app=FastAPI()
    app.include_router(build_router(service))
    with TestClient(app,base_url='http://banking.example.invalid') as client:
        page=client.get('/connections')
        assert page.status_code==200
        assert 'Connections' in page.text
        assert 'Bank connections are disabled' in page.text
        assert client.get('/bank-feed').status_code==200
        assert not (tmp_path/'private').exists()
        assert client.post('/connections/data/member',json={'name':'Generated Member'}).status_code==403
        csrf=client.get('/connections/data/overview').json()['csrf']
        headers={'Origin':'http://evil.invalid','X-Banking-CSRF':csrf}
        assert client.post('/connections/data/member',json={'name':'Generated Member'},headers=headers).status_code==403
        headers['Origin']='http://banking.example.invalid'
        assert client.post('/connections/data/member',json={'name':'Generated Member'},headers=headers).status_code==200
        assert any(m['name']=='Generated Member' for m in client.get('/connections/data/overview').json()['members'])
        assert client.post('/connections/data/link/start',json={'member_id':1,'public_token':'arbitrary'},headers=headers).status_code==422
