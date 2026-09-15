"""Deterministic, no-model adapters for structured statement exports."""

from .csv_adapter import parse_mapped_csv
from .ofx_adapter import parse_ofx
from .pdf_adapter import PDF_VERSION, detect_profile, parse_pdf_statement
from .pdf_profiles import PROFILES, PROFILES_BY_ID, StatementProfile
from .types import (
    AdapterLimits,
    ImportDiagnostic,
    ImportMetadata,
    MappedCsvV1,
    ParsedStatement,
    ParsedStatementRow,
    StructuredImportError,
)

__all__ = [
    "AdapterLimits",
    "ImportDiagnostic",
    "ImportMetadata",
    "MappedCsvV1",
    "ParsedStatement",
    "ParsedStatementRow",
    "StructuredImportError",
    "PDF_VERSION",
    "PROFILES",
    "PROFILES_BY_ID",
    "StatementProfile",
    "detect_profile",
    "parse_mapped_csv",
    "parse_ofx",
    "parse_pdf_statement",
]
