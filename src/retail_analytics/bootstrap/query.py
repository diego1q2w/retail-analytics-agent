"""Composition of query compilation and the result privacy boundary."""

from __future__ import annotations

from retail_analytics.adapters.sql_compiler import (
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.result_privacy import ResultPrivacyBoundary
from retail_analytics.bootstrap.config import BackendSettings


def build_query_compilers(
    settings: BackendSettings, *, dataset: str = PUBLIC_DATASET
) -> ScopedSqlglotCompilers:
    """Per-executive compilers; references need RETAIL_ANALYTICS_REFERENCE_KEY."""
    key = settings.reference_key
    keyring = None if key is None else ReferenceKeyring(key.get_secret_value().encode())
    return ScopedSqlglotCompilers(dataset, keyring)


def build_result_boundary() -> ResultPrivacyBoundary:
    return ResultPrivacyBoundary()
