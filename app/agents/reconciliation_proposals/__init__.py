"""Bounded merchant-evidence search.

Only the search adapter lives here for now. It carries no database handle and
its return types cannot name a transaction, category, flow, or ledger
mutation, so evidence can be gathered without any path to a silent write.
"""
from .search import (
    MerchantSearchConfig,
    MerchantSearchError,
    MerchantSearchEvidence,
    MerchantSearchResult,
    MerchantSearchSanitizationError,
    NoMerchantSearch,
    sanitize_merchant_query,
)

__all__ = [
    "MerchantSearchConfig",
    "MerchantSearchError",
    "MerchantSearchEvidence",
    "MerchantSearchResult",
    "MerchantSearchSanitizationError",
    "NoMerchantSearch",
    "sanitize_merchant_query",
]
