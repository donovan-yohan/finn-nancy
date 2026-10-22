"""Trusted household browser UI; no access/public token is accepted from a browser."""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time


from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field


from ..db import repo_period_policy
from ..api.authority import api_session_binding, is_api_request
from ..web.templating import templates
from .client import BankingError


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Member(StrictBody):
    name: str = Field(min_length=1,max_length=100)

class Consent(StrictBody):
    member_id: int
    granted: bool
    version: str

class LinkStart(StrictBody):
    member_id: int
    connection_id: int | None = None

class LinkFinish(StrictBody):
    session_id: str = Field(min_length=1,max_length=100)

class Binding(StrictBody):
    connection_id: int
    provider_account_id: str
    account_id: int | None = None
    included: bool
    ownership: str
    owner_member_id: int | None = None
    role: str
    expected_revision: int
    new_name: str | None = None
    new_kind: str | None = None

class Connection(StrictBody):
    connection_id: int

class Decision(StrictBody):
    fingerprint: str = Field(min_length=64,max_length=64)
    operation_key: str = Field(min_length=1,max_length=180)
    action: str
    reason: str = Field(min_length=1,max_length=500)
    flow_kind: str = 'unknown'
    transaction_id: int | None = None
    acknowledge_candidates: bool = False

class Receipt(StrictBody):
    extraction_id: int
    transaction_id: int
    operation_key: str = Field(min_length=1,max_length=180)
    reason: str = Field(min_length=1,max_length=500)


def build_router(service):
    router=APIRouter()
    sessions={}
    lock=threading.Lock()
    cookie='fn_banking_session'

    def ensure_session(request, response):
        if is_api_request(request):
            return api_session_binding(request),''
        now=time.time()
        sid=request.cookies.get(cookie)
        with lock:
            for key in list(sessions):
                if sessions[key]['expires']<now:
                    del sessions[key]
            if sid not in sessions:
                if len(sessions)>=1000:
                    raise HTTPException(503,'browser_session_capacity')
                sid=secrets.token_urlsafe(32)
                sessions[sid]={'csrf':secrets.token_urlsafe(32),'expires':now+3600}
                response.set_cookie(cookie,sid,httponly=True,secure=service.origin.startswith('https://'),
                    samesite='lax',max_age=3600,path='/')
            return sid,sessions[sid]['csrf']

    def browser_write(request: Request):
        if is_api_request(request):
            # Direct callers use the same workflows; Link still retains its
            # own server-minted session binding and provider verification.
            return api_session_binding(request)
        # Never derive trust from forwarded headers, Host or a caller's Referer.
        if not service.origin or request.headers.get('origin')!=service.origin:
            raise HTTPException(403,'trusted_origin_required')
        sid=request.cookies.get(cookie,'')
        with lock:
            session=sessions.get(sid)
            if not session or session['expires']<time.time() or not secrets.compare_digest(
                request.headers.get('x-banking-csrf',''),session['csrf']):
                raise HTTPException(403,'browser_session_required')
        return sid

    def safe(call):
        try:
            return call()
        except (BankingError,repo_period_policy.PeriodPolicyError) as exc:
            # BankingError holds only internal codes; policy errors contain only local dates.
            raise HTTPException(409,str(exc)) from None
        except sqlite3.IntegrityError:
            raise HTTPException(409,'account_or_evidence_conflict') from None

    def page(request, template, context, session=None):
        response=templates.TemplateResponse(request,template,{'active':'more','brand':'finn',**context})
        if session is None:
            ensure_session(request,response)
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @router.get('/connections',response_class=HTMLResponse)
    def connections(request: Request):
        response=JSONResponse({})
        browser,_=ensure_session(request,response)
        rendered=page(request,'banking_connections.html',{'overview':service.overview(),
            'pending_links':service.browser_links(browser)},session=browser)
        for header,value in response.raw_headers:
            if header==b'set-cookie':
                rendered.raw_headers.append((header,value))
        return rendered

    @router.get('/bank-feed',response_class=HTMLResponse)
    def bank_feed(request: Request):
        return page(request,'banking_feed.html',{'rows':service.feed(),'receipts':service.pending_receipts()})

    @router.get('/bank-feed/{subject_id}',response_class=HTMLResponse)
    def review(request: Request,subject_id:int):
        return page(request,'banking_review.html',{'plan':safe(lambda:service.review_plan(subject_id))})

    @router.get('/connections/data/overview')
    def overview(request: Request):
        response=JSONResponse({})
        sid,csrf=ensure_session(request,response)
        response.body=JSONResponse({**service.overview(),'csrf':csrf}).body
        response.headers['content-length']=str(len(response.body))
        response.headers['Cache-Control']='no-store'
        return response

    @router.get('/connections/data/feed')
    def feed_data():
        return JSONResponse({'rows':service.feed()},headers={'Cache-Control':'no-store'})

    @router.get('/connections/data/plan/{subject_id}')
    def plan_data(subject_id:int):
        return JSONResponse(safe(lambda:service.review_plan(subject_id)),headers={'Cache-Control':'no-store'})

    @router.post('/connections/data/member')
    def member(body:Member,_=Depends(browser_write)):
        return safe(lambda:service.add_member(body.name))

    @router.post('/connections/data/consent')
    def consent(body:Consent,_=Depends(browser_write)):
        return safe(lambda:service.consent(**body.model_dump()))

    @router.post('/connections/data/link/start')
    def start(body:LinkStart,browser=Depends(browser_write)):
        return safe(lambda:service.start_link(**body.model_dump(),browser=browser))

    @router.post('/connections/data/link/finish')
    def finish(body:LinkFinish,browser=Depends(browser_write)):
        return safe(lambda:service.finish_link(body.session_id,browser=browser))

    @router.post('/connections/data/bind')
    def bind(body:Binding,_=Depends(browser_write)):
        return safe(lambda:service.bind(**body.model_dump()))

    @router.post('/connections/data/accounts')
    def discover(body:Connection,_=Depends(browser_write)):
        return safe(lambda:service.discover_accounts(body.connection_id))

    @router.post('/connections/data/sync')
    def sync(body:Connection,_=Depends(browser_write)):
        return safe(lambda:service.sync(body.connection_id))

    @router.post('/connections/data/disconnect')
    def disconnect(body:Connection,_=Depends(browser_write)):
        return safe(lambda:service.disconnect(body.connection_id))

    @router.post('/connections/data/decision/{subject_id}')
    def decide(subject_id:int,body:Decision,_=Depends(browser_write)):
        return safe(lambda:service.decide(subject_id,**body.model_dump()))

    @router.post('/connections/data/receipt')
    def receipt(body:Receipt,_=Depends(browser_write)):
        return safe(lambda:service.attach_receipt(**body.model_dump()))

    return router
