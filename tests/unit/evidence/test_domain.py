"""Pure evidence rules: encoding, digests and the reuse policy."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.evidence import (
    MAX_PAYLOAD_BYTES,
    AnalysisStamp,
    AuthorityStamp,
    CurrentAuthority,
    DefinitionRef,
    Evidence,
    EvidenceColumn,
    EvidenceContent,
    EvidenceError,
    EvidenceKind,
    EvidenceTable,
    Provenance,
    Requirements,
    ReuseBlock,
    ReuseIntent,
    ReusePolicy,
    check_bounds,
    content_digest,
    decode_analysis,
    decode_cell,
    decode_provenance,
    decode_table,
    encode_analysis,
    encode_cell,
    encode_provenance,
    encode_table,
    snapshot,
)
from retail_analytics.domain.periods import DateWindow

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
SCOPE = ProductScope(frozenset({"1", "3"}), 4)
REVENUE = DefinitionRef("completed_item_sales", 1)
WINDOW = DateWindow(date(2026, 9, 1), date(2026, 10, 1))


def _content(**changes: object) -> EvidenceContent:
    content = EvidenceContent(
        kind=EvidenceKind.QUERY,
        subject_key="q:abc",
        analysis=AnalysisStamp(1, 1, frozenset({REVENUE}), "fp", WINDOW),
        provenance=Provenance(
            logical_sql="SELECT category, SUM(x) AS total FROM t GROUP BY category",
            executed_query_digest="d" * 64,
        ),
        table=EvidenceTable(
            columns=(
                EvidenceColumn("category", "value"),
                EvidenceColumn("total", "value"),
            ),
            rows=(("Jeans", Decimal("10.50")), ("Tops", Decimal("3"))),
            received_rows=2,
        ),
        grain=("category",),
    )
    return replace(content, **changes)  # type: ignore[arg-type]


def _evidence(
    content: EvidenceContent | None = None,
    *,
    computed_at: datetime = NOW,
    executive: str = "exec-a",
    session: str = "ses-a",
    scope: ProductScope = SCOPE,
) -> Evidence:
    content = content or _content()
    return Evidence(
        evidence_id="evd1",
        lineage_id="evd1",
        version=1,
        executive_id=executive,
        session_id=session,
        run_id="run-1",
        operation_id="op-1",
        authority=AuthorityStamp.of(scope),
        content=content,
        computed_at=computed_at,
        content_digest=content_digest(content, computed_at),
    )


AUTHORITY = CurrentAuthority("exec-a", "ses-a", SCOPE)
REQ = Requirements(1, 1, "fp", frozenset({REVENUE}), WINDOW)


def _assess(
    evidence: Evidence,
    *,
    authority: CurrentAuthority = AUTHORITY,
    requirements: Requirements = REQ,
    intent: ReuseIntent = ReuseIntent.CURRENT,
    now: datetime = NOW,
    invalidated: bool = False,
) -> ReuseBlock | None:
    return ReusePolicy().assess(
        evidence,
        authority=authority,
        requirements=requirements,
        intent=intent,
        now=now,
        invalidated=invalidated,
    )


@pytest.mark.parametrize(
    "cell",
    [
        None,
        True,
        0,
        -7,
        2**70,
        1.5,
        float("inf"),
        float("-inf"),
        "text",
        Decimal("12.3400"),
        date(2026, 9, 30),
        datetime(2026, 9, 30, 8, 1, 2, 345678, tzinfo=UTC),
        datetime(2026, 9, 30, 8, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_cells_round_trip_through_json(cell: object) -> None:
    encoded = json.loads(json.dumps(encode_cell(cell)))  # type: ignore[arg-type]
    decoded = decode_cell(encoded)
    assert decoded == cell
    assert type(decoded) is type(cell)


def test_negative_zero_and_naive_timestamps() -> None:
    assert repr(encode_cell(-0.0)) == "0.0"
    with pytest.raises(EvidenceError):
        encode_cell(datetime(2026, 1, 1))


def test_documents_round_trip() -> None:
    content = _content()
    assert decode_table(json.loads(json.dumps(encode_table(content.table)))) == (
        content.table
    )
    assert decode_provenance(encode_provenance(content.provenance)) == (
        content.provenance
    )
    assert decode_analysis(encode_analysis(content.analysis)) == content.analysis


def test_digest_detects_changed_content_and_ignores_timezone_spelling() -> None:
    evidence = _evidence()
    assert evidence.is_intact
    other_zone = NOW.astimezone(timezone(timedelta(hours=-5)))
    assert content_digest(evidence.content, other_zone) == evidence.content_digest
    altered = replace(
        evidence.content,
        table=replace(evidence.content.table, rows=(("Jeans", Decimal("99")),)),
    )
    assert not replace(evidence, content=altered).is_intact
    assert _assess(replace(evidence, content=altered)) is ReuseBlock.TAMPERED


def test_content_invariants() -> None:
    with pytest.raises(EvidenceError):
        _content(grain=("missing",))
    with pytest.raises(EvidenceError):
        _content(provenance=Provenance())
    with pytest.raises(EvidenceError):
        _content(kind=EvidenceKind.DERIVED)
    with pytest.raises(EvidenceError):
        EvidenceTable((EvidenceColumn("a", "value"),), (("x", "y"),), 1)
    with pytest.raises(EvidenceError):
        AuthorityStamp.of(ProductScope(frozenset(), 3))


def test_payload_bound() -> None:
    rows = tuple((f"c{i}", "x" * 1000) for i in range(MAX_PAYLOAD_BYTES // 1000))
    big = _content(
        table=EvidenceTable(
            (EvidenceColumn("category", "value"), EvidenceColumn("total", "value")),
            rows,
            len(rows),
        )
    )
    with pytest.raises(EvidenceError):
        check_bounds(big)
    check_bounds(_content())


def test_reusable_when_everything_matches() -> None:
    assert _assess(_evidence()) is None


def test_freshness_applies_to_current_questions_only() -> None:
    evidence = _evidence()
    limit = NOW + timedelta(minutes=15)
    assert _assess(evidence, now=limit) is None
    later = limit + timedelta(seconds=1)
    assert _assess(evidence, now=later) is ReuseBlock.STALE
    assert _assess(evidence, now=later, intent=ReuseIntent.EXPLAIN) is None
    assert _assess(evidence, intent=ReuseIntent.REFRESH) is ReuseBlock.REFRESH_REQUESTED
    shot = snapshot(evidence, later)
    assert shot.age == timedelta(minutes=15, seconds=1)
    assert "2026-10-08 12:00 UTC" in shot.describe()
    assert "2026-09-01 to 2026-09-30" in shot.describe()


def test_configurable_freshness() -> None:
    policy = ReusePolicy(timedelta(minutes=1))
    block = policy.assess(
        _evidence(),
        authority=AUTHORITY,
        requirements=REQ,
        intent=ReuseIntent.CURRENT,
        now=NOW + timedelta(minutes=2),
    )
    assert block is ReuseBlock.STALE


@pytest.mark.parametrize(
    ("authority", "block"),
    [
        (CurrentAuthority("exec-b", "ses-a", SCOPE), ReuseBlock.NOT_OWNED),
        (CurrentAuthority("exec-a", "ses-other", SCOPE), ReuseBlock.OTHER_SESSION),
        (
            CurrentAuthority("exec-a", "ses-a", ProductScope(frozenset(), 4)),
            ReuseBlock.NO_PRODUCT_SCOPE,
        ),
        (
            CurrentAuthority("exec-a", "ses-a", ProductScope(SCOPE.product_ids, 5)),
            ReuseBlock.AUTHORIZATION_CHANGED,
        ),
        (
            CurrentAuthority("exec-a", "ses-a", ProductScope(frozenset({"1"}), 4)),
            ReuseBlock.AUTHORIZATION_CHANGED,
        ),
    ],
)
def test_authority_blocks_come_first(
    authority: CurrentAuthority, block: ReuseBlock
) -> None:
    # Even an explanation of an old snapshot needs current authority.
    assert _assess(_evidence(), authority=authority, intent=ReuseIntent.EXPLAIN) is (
        block
    )
    assert block.is_authority


def test_invalidated_evidence_is_not_reused() -> None:
    assert _assess(_evidence(), invalidated=True) is ReuseBlock.INVALIDATED


@pytest.mark.parametrize(
    ("requirements", "block"),
    [
        (replace(REQ, catalog_version=2), ReuseBlock.CATALOG_CHANGED),
        (replace(REQ, policy_version=2), ReuseBlock.POLICY_CHANGED),
        (
            replace(
                REQ, definitions=frozenset({DefinitionRef("completed_item_sales", 2)})
            ),
            ReuseBlock.DEFINITIONS_CHANGED,
        ),
        (replace(REQ, preference_fingerprint="other"), ReuseBlock.PREFERENCES_CHANGED),
        (
            replace(REQ, period=DateWindow(date(2026, 8, 1), date(2026, 9, 1))),
            ReuseBlock.PERIOD_MISMATCH,
        ),
        (replace(REQ, time_zone="Europe/Madrid"), ReuseBlock.TIME_ZONE_MISMATCH),
        (replace(REQ, grain=frozenset({"brand"})), ReuseBlock.INSUFFICIENT_GRANULARITY),
    ],
)
def test_meaning_and_granularity_blocks(
    requirements: Requirements, block: ReuseBlock
) -> None:
    assert _assess(_evidence(), requirements=requirements) is block


def test_fewer_definitions_and_coarser_grain_are_fine() -> None:
    assert _assess(_evidence(), requirements=replace(REQ, definitions=frozenset())) is (
        None
    )
    assert _assess(_evidence(), requirements=replace(REQ, grain=frozenset())) is None


def test_truncated_rows_cannot_feed_a_calculation() -> None:
    truncated = _content(
        table=replace(_content().table, truncation="rows", received_rows=900)
    )
    evidence = _evidence(truncated)
    complete = replace(REQ, requires_complete=True)
    assert _assess(evidence, requirements=complete) is ReuseBlock.TRUNCATED
    # Explaining what was shown is still fine.
    assert _assess(evidence, intent=ReuseIntent.EXPLAIN) is None


def test_future_computation_counts_as_fresh() -> None:
    evidence = _evidence(computed_at=NOW + timedelta(minutes=1))
    assert snapshot(evidence, NOW).age == timedelta(0)
    assert _assess(evidence) is None
