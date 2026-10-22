"""Authenticated proposal-only finance router; composition belongs to web.app."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from . import analysis, finance
from .auth import require_api_token
from .finance_schemas import CategoryProposal, FinancialQuery, HttpId, TransactionSearch

router = APIRouter(prefix="/api/finance", tags=["finance"], dependencies=[Depends(require_api_token)])


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except finance.OperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/transactions")
def transactions(filters: Annotated[TransactionSearch, Query()]):
    return _call(finance.search_transactions, **filters.model_dump(mode="json"))


@router.get("/transactions/{transaction_id}")
def transaction(transaction_id: HttpId):
    return _call(finance.transaction_detail, transaction_id)


@router.get("/categories")
def categories():
    return finance.categories()


@router.get("/schema")
def financial_schema():
    return analysis.schema()


class _FinancialQueryRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Invalid input may itself be unserializable (inf or a lone
                # surrogate). Never echo it through the framework error response.
                raise HTTPException(422, 'query input is invalid') from None

        return sanitized_handler


def financial_query(body: FinancialQuery):
    return _call(analysis.query, **body.model_dump())


router.add_api_route('/query', financial_query, methods=['POST'], route_class_override=_FinancialQueryRoute)


@router.post("/category-proposals")
def propose(body: CategoryProposal):
    return _call(finance.propose_category, **body.model_dump())


@router.get("/proposals/{proposal_id}")
def proposal(proposal_id: HttpId):
    return _call(finance.proposal_detail, proposal_id)
