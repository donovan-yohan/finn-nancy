"""Versioned merchant-descriptor normalization for scoped resolution knowledge.

This is intentionally separate from ``app.ingest.normalize.norm_merchant``.
That legacy normalizer participates in statement row identity and cannot be
changed without invalidating already-staged evidence.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass

NORMALIZATION_VERSION = "descriptor-v2"
MAX_DESCRIPTOR_CHARS = 512
MAX_TOKENS = 24
MAX_TOKEN_CHARS = 64


class DescriptorNormalizationError(ValueError):
    """The descriptor cannot be represented by the bounded v2 grammar."""


@dataclass(frozen=True)
class NormalizedDescriptor:
    version: str
    tokens: tuple[str, ...]
    value: str
    fingerprint: str

    @property
    def pattern_json(self) -> str:
        return json.dumps(
            list(self.tokens),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def normalize_descriptor_v2(raw: str) -> NormalizedDescriptor:
    """Return bounded Unicode-aware tokens while preserving meaningful digits."""
    source = str(raw or "")
    if len(source) > MAX_DESCRIPTOR_CHARS:
        raise DescriptorNormalizationError("descriptor exceeds 512 characters")

    normalized = unicodedata.normalize("NFKC", source).casefold()
    token_chars: list[str] = []
    tokens: list[str] = []

    def flush() -> None:
        if not token_chars:
            return
        token = "".join(token_chars)
        token_chars.clear()
        if len(token) > MAX_TOKEN_CHARS:
            raise DescriptorNormalizationError("descriptor token exceeds 64 characters")
        tokens.append(token)

    for char in normalized:
        if char.isalnum():
            token_chars.append(char)
        else:
            flush()
    flush()

    if not tokens:
        raise DescriptorNormalizationError("descriptor has no alphanumeric tokens")
    if len(tokens) > MAX_TOKENS:
        raise DescriptorNormalizationError("descriptor exceeds 24 tokens")

    token_tuple = tuple(tokens)
    canonical = json.dumps(
        {
            "normalization_version": NORMALIZATION_VERSION,
            "pattern_kind": "exact_tokens",
            "tokens": token_tuple,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return NormalizedDescriptor(
        version=NORMALIZATION_VERSION,
        tokens=token_tuple,
        value=" ".join(token_tuple),
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )
