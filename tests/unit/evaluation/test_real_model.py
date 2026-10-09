"""Real-model evaluation parts: provider attribution, figure checks, summary."""

from __future__ import annotations

from retail_analytics.adapters.evaluation.telemetry_recorder import (
    RecordingTelemetrySink,
)
from retail_analytics.application.contracts.evaluation import (
    ConversationOutcome,
    ObservedTable,
    RealModelEvaluation,
    RecordedSpan,
)
from retail_analytics.application.contracts.telemetry import Span
from retail_analytics.application.evaluation.manifest import Scenario
from retail_analytics.application.evaluation.real_model import (
    figure_checks,
    number_in_text,
    provider_use,
    render_summary,
)
from retail_analytics.application.telemetry import Telemetry, use_telemetry

PRIMARY = "google-interactions"


def attempt(
    run: str, provider: str, outcome: str, origin: str = "none"
) -> RecordedSpan:
    return RecordedSpan(
        Span.MODEL_ATTEMPT.value,
        run,
        {
            "provider": provider,
            "model": "m",
            "outcome": outcome,
            "fallback_reason": "other" if origin != "none" else "none",
        },
    )


def answered(run: str, provider: str) -> RecordedSpan:
    return RecordedSpan(Span.RUN.value, run, {"answered_by": provider})


def test_only_primary_service_counts_as_primary() -> None:
    spans = [attempt("r1", PRIMARY, "succeeded"), answered("r1", PRIMARY)]
    use = provider_use(spans, ["r1"], PRIMARY)
    assert use.label == "primary"
    assert use.answered_by == (PRIMARY,)
    assert use.attempts == {f"{PRIMARY}:m:succeeded": 1}


def test_fallback_and_mixed_runs_are_labelled() -> None:
    fallback = [
        attempt("r1", PRIMARY, "failed"),
        attempt("r1", "openai", "succeeded", origin=PRIMARY),
        answered("r1", "openai"),
    ]
    use = provider_use(fallback, ["r1"], PRIMARY)
    assert use.label == "fallback"
    assert use.fallback_reasons == ("other",)
    mixed = [*fallback, attempt("r2", PRIMARY, "succeeded"), answered("r2", PRIMARY)]
    assert provider_use(mixed, ["r1", "r2"], PRIMARY).label == "mixed"
    # Spans of other conversations are ignored.
    assert provider_use(mixed, ["r2"], PRIMARY).label == "primary"
    assert provider_use([], ["r1"], PRIMARY).label == "unattributed"


def test_recorder_keeps_sanitized_span_attributes() -> None:
    sink = RecordingTelemetrySink()
    with (
        use_telemetry(Telemetry(sink)),
        Telemetry(sink).span(
            Span.MODEL_ATTEMPT, run_id="run-1", attributes={"provider": PRIMARY}
        ) as span,
    ):
        span.set({"outcome": "succeeded", "sql": "SELECT secret"})
    (recorded,) = sink.spans()
    assert recorded.run_id == "run-1"
    assert recorded.attributes == {"provider": PRIMARY, "outcome": "succeeded"}


def test_stated_numbers_allow_display_rounding_only() -> None:
    assert number_in_text(73164.81, 0.01, "Revenue was $73,164.81.", share=False)
    assert number_in_text(73164.81, 0.01, "about 73.2k in revenue", share=False)
    assert number_in_text(0.065, 0.0005, "the top ten hold 6.5% of spend", share=True)
    assert number_in_text(-545.8, 0.01, "revenue fell by 545.80", share=False)
    assert not number_in_text(73164.81, 0.01, "about 70k in revenue", share=False)
    assert not number_in_text(73164.81, 0.01, "Revenue was 73,164.18", share=False)
    assert not number_in_text(886, 0, "evd_886abc and 2025-08-86", share=False)


def scenario() -> Scenario:
    return Scenario.model_validate(
        {
            "id": "rd-x",
            "title": "t",
            "level": 1,
            "category": "products",
            "verification": ["deterministic"],
            "scope": {"executive_ref": "e"},
            "dialogue": [{"text": "q"}],
            "expectations": [
                {"kind": "numeric", "name": "top_share", "expected": 0.065},
                {"kind": "exact", "name": "units", "expected": 12},
                {"kind": "exact", "name": "product_1", "expected": "Tops & Tees"},
                {"kind": "exact", "name": "pii_released", "expected": False},
                {"kind": "contains", "name": "names", "needle": "Sweaters"},
                {"kind": "not_contains", "name": "canary", "needle": "x@y.z"},
            ],
        }
    )


def table(rows: tuple[tuple[object, ...], ...]) -> ObservedTable:
    width = len(rows[0])
    return ObservedTable(
        evidence_id="evd_1",
        columns=tuple(f"c{i}" for i in range(width)),
        roles=("value",) * width,
        sources=((),) * width,
        rows=rows,  # type: ignore[arg-type]
        truncated=False,
        scope_matches=True,
    )


def test_figures_are_checked_in_evidence_and_text_independently() -> None:
    tables = [table((("tops & tees", 6.5, 12),))]
    checks = {c.name: c for c in figure_checks(scenario(), "Tops & Tees led.", tables)}
    assert set(checks) == {"top_share", "units", "product_1", "names"}
    assert checks["top_share"].in_evidence is True  # stored as a percent
    assert checks["top_share"].in_answer is False  # evidence alone is not enough
    assert checks["units"].in_evidence is True
    assert checks["product_1"].in_evidence and checks["product_1"].in_answer
    assert checks["names"].in_evidence is None and not checks["names"].in_answer


def test_labels_match_across_dash_styles() -> None:
    data = scenario().model_dump()
    data["expectations"] = [{"kind": "exact", "name": "band", "expected": "65-69"}]
    band = Scenario.model_validate(data)
    text = "Age 65\u201369 spent most."
    (check,) = figure_checks(band, text, [table((("65\u201369",),))])
    assert check.in_answer and check.in_evidence


def test_summary_separates_primary_from_fallback() -> None:
    def outcome(sid: str, label: str) -> ConversationOutcome:
        return ConversationOutcome.model_validate(
            {
                "scenario_id": sid,
                "suite": "s",
                "category": "c",
                "level": 1,
                "judge_required": False,
                "turns": 1,
                "runner_status": "failed",
                "figures": [
                    {"name": "a", "kind": "number", "expected": 1.0, "in_answer": True}
                ],
                "provider": {"label": label},
            }
        )

    result = RealModelEvaluation(
        recorded_on="2026-10-09",
        code_version="abc",
        target_id="agent_runtime:local",
        execution_backend="local",
        primary_provider=PRIMARY,
        conversations=(outcome("one", "primary"), outcome("two", "fallback")),
    )
    text = render_summary(result)
    assert "(1 conversations)" in text
    assert "not counted as primary" in text
    assert "| two | s | fallback |" in text
