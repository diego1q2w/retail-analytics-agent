"""Answers citing saved-report evidence carry its source and date (T18-F2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from retail_analytics.application.investigation_runtime import _source_notes
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.evidence import ReportSource
from tests.unit.evidence.support import (
    SCOPE_A,
    Env,
    basis,
    compiled_and_released,
    operation,
)
from tests.unit.evidence.support import context as exec_context

pytestmark = pytest.mark.asyncio


async def test_only_report_evidence_gets_a_source_line() -> None:
    env = Env()
    compiled, released = compiled_and_released(scope=SCOPE_A)
    evidence = await env.service.record_query(
        operation(exec_context(), "op-1"), compiled, released, basis()
    )
    source = ReportSource("rep-1", 3, "Q3 review", datetime(2026, 10, 9, tzinfo=UTC))

    assert _source_notes([EvidenceStanding(evidence, None)]) == ""
    notes = _source_notes([EvidenceStanding(evidence, None, source)])
    assert notes.startswith(
        "\n\nSources from saved reports:\n- " + evidence.evidence_id
    )
    assert 'from saved report "Q3 review" (v3), computed 2026-10-08 12:00 UTC' in notes
    assert notes.endswith("historical snapshot, not current data")
