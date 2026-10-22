"""Generated canonical composition. MockTransport only; socket egress is denied."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from fastapi import HTTPException


def generated_banking(root: Path, origin: str):
    root.mkdir(parents=True,exist_ok=True)
    credential=root/'generated-credentials.json'
    credential.write_text(json.dumps({'client_id':secrets.token_hex(12),'secret':secrets.token_hex(24)}))
    credential.chmod(0o600)
    os.environ.update(DB_PATH=str(root/'banking.sqlite'),DATA_DIR=str(root),
        PLAID_ENABLED='true',STRICT_LOCAL_MODE='false',READ_ONLY='false',
        PLAID_CREDENTIALS_FILE=str(credential),PLAID_PRIVATE_DIR=str(root/'private'),
        PLAID_ORIGIN=origin,PLAID_COMPLETION_URI=origin+'/connections',API_TOKEN='generated-browser-finance')
    from app.config import Settings,get_settings
    Settings.model_config['env_file']=None
    get_settings.cache_clear()
    from app.db import engine,migrate
    migrate.init_db(root/'banking.sqlite')
    with engine.write_tx(root/'banking.sqlite') as conn:
        conn.execute('DELETE FROM household_members')
    from app.web.app import create_app
    app=create_app()
    service=app.state.banking_service
    links={}
    dispatched=[]
    def provider(request):
        body=json.loads(request.content)
        dispatched.append(request.url.path)
        assert request.url.host=='production.plaid.com'
        if request.url.path=='/link/token/create':
            index=len(links)+1
            token='generated-link-'+secrets.token_hex(16)
            links[token]={'index':index,'finished':False,'public':'generated-public-'+secrets.token_hex(16)}
            return httpx.Response(200,json={'link_token':token,
                'hosted_link_url':f'https://secure.plaid.com/hl/generated-{index}',
                'expiration':(datetime.now(timezone.utc)+timedelta(minutes=25)).isoformat()})
        if request.url.path=='/link/token/get':
            link=links[body['link_token']]
            sessions=[] if not link['finished'] else [{'link_session_id':f"generated-session-{link['index']}",
                'finished_at':datetime.now(timezone.utc).isoformat(),
                'on_success':{'public_token':link['public'],'metadata':{}},
                'results':{'item_add_results':[{'public_token':link['public']}]}}]
            return httpx.Response(200,json={'link_token':body['link_token'],'link_sessions':sessions})
        if request.url.path=='/item/public_token/exchange':
            link=next(v for v in links.values() if v['public']==body['public_token'])
            return httpx.Response(200,json={'access_token':f"generated-access-{link['index']}", 'item_id':f"generated-item-{link['index']}"})
        index=int(body['access_token'].rsplit('-',1)[1])
        if request.url.path=='/accounts/get':
            return httpx.Response(200,json={'accounts':[{'account_id':f'generated-account-{index}',
                'name':'Generated joint chequing','type':'depository','subtype':'checking','balances':{'iso_currency_code':'CAD'}}]})
        if request.url.path=='/transactions/sync':
            rows=[] if body.get('cursor') else [{'transaction_id':f'generated-txn-{index}',
                'account_id':f'generated-account-{index}','amount':12.34,'date':'2026-06-12',
                'iso_currency_code':'CAD','pending':False,'name':'GENERATED CAFE ORIGINAL','merchant_name':'Generated Cafe'}]
            return httpx.Response(200,json={'added':rows,'modified':[],'removed':[],'has_more':False,'next_cursor':f'cursor-{index}'})
        if request.url.path=='/item/remove':
            return httpx.Response(200,json={'removed':True})
        raise AssertionError(request.url.path)
    service.transport=httpx.MockTransport(provider)
    @app.post('/__banking_fixture/hosted/{index}')
    def complete_hosted(index:int):
        link=next(v for v in links.values() if v['index']==index)
        link['finished']=True
        return {'status':'generated_hosted_completed'}
    @app.get('/__banking_fixture')
    def manifest():
        with engine.read_conn(service.db_path) as conn:
            return {'members':conn.execute('SELECT COUNT(*) FROM household_members').fetchone()[0],
                'transactions':conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],
                'associations':conn.execute('SELECT COUNT(*) FROM bank_associations').fetchone()[0],
                'dispatched':dispatched,'fixture_only':True}
    original_connect=socket.socket.connect
    def local_connect(sock,address):
        if sock.family in (socket.AF_INET,socket.AF_INET6):
            try:
                allowed=ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                allowed=address[0]=='localhost'
            if not allowed:
                raise RuntimeError('Generated banking acceptance forbids network egress')
        return original_connect(sock,address)
    socket.socket.connect=local_connect
    return app
