"""Per model request: provider, model id, attempt, fallback and who answered."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse

from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.investigation_runtime import RunStopped
from retail_analytics.application.telemetry import (
    ATTRIBUTION_METADATA_KEY,
    attribution_from_metadata,
    trace_id_for,
    use_telemetry,
)
from tests.unit.models import stubs
from tests.unit.models.test_provider_chain import (
    gemini_answer,
    gpt_answer,
    harness,
)
from tests.unit.telemetry.recording import recording

pytestmark = pytest.mark.asyncio
GEMINI = "google-interactions"
OPENAI = "openai"


def served_by(result: object) -> object:
    last = [m for m in result.all_messages() if isinstance(m, ModelResponse)][-1]  # type: ignore[attr-defined]
    return attribution_from_metadata(
        (last.metadata or {}).get(ATTRIBUTION_METADATA_KEY)
    )


async def test_primary_answer_is_attributed_to_the_primary() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder([gemini_answer()])
    with use_telemetry(telemetry):
        result = await harness(gemini, stubs.Recorder([])).run()
    (attempt,) = sink.named(Span.MODEL_ATTEMPT)
    assert attempt.run_id == "run-1"
    assert attempt.attributes["provider"] == GEMINI
    assert attempt.attributes["model"]
    assert attempt.attributes["attempt"] == 1
    assert attempt.attributes["fallback_from"] == "none"
    assert attempt.attributes["outcome"] == "succeeded"
    assert attempt.attributes["input_tokens"] == 100
    (request,) = sink.named(Span.MODEL_REQUEST)
    assert request.attributes["answered_by"] == GEMINI
    assert request.attributes["fallback"] is False
    assert sink.total(Metric.MODEL_REQUESTS, provider=GEMINI, outcome="succeeded") == 1
    assert sink.total(Metric.MODEL_TOKENS, provider=GEMINI, direction="input") == 100
    assert sink.total(Metric.MODEL_FALLBACKS) == 0
    served = served_by(result)
    assert served.provider == GEMINI and served.fallback_from is None  # type: ignore[attr-defined]


async def test_outage_records_each_attempt_the_fallback_and_the_answering_backup() -> (
    None
):
    telemetry, sink = recording()
    gemini = stubs.Recorder([stubs.error(503, "unavailable")] * 3)
    gpt = stubs.Recorder([gpt_answer()])
    with use_telemetry(telemetry):
        result = await harness(gemini, gpt).run()

    attempts = sink.named(Span.MODEL_ATTEMPT)
    assert [(a.attributes["provider"], a.attributes["attempt"]) for a in attempts] == [
        (GEMINI, 1),
        (GEMINI, 2),
        (GEMINI, 3),
        (OPENAI, 1),
    ]
    assert [a.attributes["outcome"] for a in attempts] == ["failed"] * 3 + ["succeeded"]
    assert {a.attributes["reason_class"] for a in attempts[:3]} == {"server_error"}
    assert [a.attributes["request_sequence"] for a in attempts] == [1, 2, 3, 4]
    backup = attempts[-1]
    assert backup.attributes["fallback_from"] == GEMINI
    assert backup.attributes["fallback_reason"] == "server_error"
    assert sink.total(Metric.MODEL_REQUESTS, provider=GEMINI, outcome="failed") == 3
    assert (
        sink.total(
            Metric.MODEL_FALLBACKS,
            from_provider=GEMINI,
            to_provider=OPENAI,
            reason_class="server_error",
        )
        == 1
    )
    (request,) = sink.named(Span.MODEL_REQUEST)
    assert request.attributes["answered_by"] == OPENAI
    assert request.attributes["attempts"] == 4
    assert request.attributes["fallback"] is True
    served = served_by(result)
    assert (served.provider, served.fallback_from, served.fallback_reason) == (  # type: ignore[attr-defined]
        OPENAI,
        GEMINI,
        "server_error",
    )
    # Every span of the run shares the run's trace through its run id.
    assert {s.run_id for s in sink.spans} == {"run-1"}
    assert trace_id_for("run-1")


async def test_retry_within_the_primary_reports_the_attempt_number() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder(
        [
            stubs.error(429, "resource_exhausted", details=[{"retryDelay": "0s"}]),
            gemini_answer(),
        ]
    )
    with use_telemetry(telemetry):
        result = await harness(gemini, stubs.Recorder([])).run()
    first, second = sink.named(Span.MODEL_ATTEMPT)
    assert first.attributes["reason_class"] == "rate_limited"
    assert (
        second.attributes["attempt"] == 2
        and second.attributes["outcome"] == "succeeded"
    )
    assert sink.total(Metric.MODEL_FALLBACKS) == 0
    served = served_by(result)
    assert served.attempt == 2 and served.fallback_from is None  # type: ignore[attr-defined]


async def test_a_cooling_primary_is_skipped_and_reported_as_the_fallback_reason() -> (
    None
):
    telemetry, sink = recording()
    gemini = stubs.Recorder(
        [stubs.error(429, "resource_exhausted", details=[{"retryDelay": "40s"}])]
    )
    gpt = stubs.Recorder([gpt_answer("first"), gpt_answer("second")])
    h = harness(gemini, gpt)
    with use_telemetry(telemetry):
        await h.run()
        await h.run()
    assert (
        sink.total(
            Metric.MODEL_FALLBACKS, from_provider=GEMINI, reason_class="rate_limited"
        )
        == 1
    )
    assert (
        sink.total(
            Metric.MODEL_FALLBACKS, from_provider=GEMINI, reason_class="cooling_down"
        )
        == 1
    )
    assert sink.total(Metric.MODEL_REQUESTS, provider=GEMINI, outcome="skipped") == 1
    assert len(gemini.requests) == 1  # skipped: no request was sent


async def test_unavailable_chain_is_reported_as_exhausted() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder([stubs.error(503, "unavailable")] * 3)
    gpt = stubs.Recorder([stubs.error(401, "invalid_api_key")])
    with use_telemetry(telemetry), pytest.raises(RunStopped):
        await harness(gemini, gpt).run()
    (request,) = sink.named(Span.MODEL_REQUEST)
    assert request.attributes["outcome"] == "exhausted"
    assert (
        sink.total(Metric.MODEL_REQUESTS, provider=OPENAI, reason_class="rejected") == 1
    )
    assert sink.total(Metric.MODEL_FALLBACKS) == 0


async def test_no_secret_or_personal_data_reaches_model_telemetry() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder([stubs.error(401, "unauthenticated")])
    gpt = stubs.Recorder([gpt_answer("Sales were 10.")])
    with use_telemetry(telemetry):
        await harness(gemini, gpt).run("Sales last month for maria.canary@example.com?")
    text = sink.everything()
    for secret in (stubs.GEMINI_KEY, stubs.OPENAI_KEY, "maria.canary"):
        assert secret not in text
    # Sanitized content is captured by design (T30-F2): the answer is visible.
    assert "Sales were 10" in text
    labels = {k for _, _, labels in sink.counts for k in labels}
    assert labels <= set(Label)


async def test_disabled_content_capture_keeps_metadata_only() -> None:
    telemetry, sink = recording(capture_content=False)
    gemini = stubs.Recorder([stubs.error(401, "unauthenticated")])
    gpt = stubs.Recorder([gpt_answer("Sales were 10.")])
    with use_telemetry(telemetry):
        await harness(gemini, gpt).run("Sales last month?")
    text = sink.everything()
    assert "Sales" not in text
    attempts = sink.named(Span.MODEL_ATTEMPT)
    assert [a.attributes["outcome"] for a in attempts] == ["failed", "succeeded"]
    assert all(a.attributes["content_capture"] == "disabled" for a in attempts)
    assert all(not a.payloads for a in sink.spans)
    assert attempts[1].attributes["input_tokens"] == 50


async def test_connection_lost_mid_stream_is_attributed_as_a_connection_failure() -> (
    None
):
    telemetry, sink = recording()
    cut = stubs.broken_stream(stubs.gemini_text("partial")[:2])
    gemini = stubs.Recorder([cut] * 3)
    with use_telemetry(telemetry):
        result = await harness(gemini, stubs.Recorder([gpt_answer()])).run()

    attempts = sink.named(Span.MODEL_ATTEMPT)
    assert [a.attributes["outcome"] for a in attempts] == ["failed"] * 3 + ["succeeded"]
    assert {a.attributes["reason_class"] for a in attempts[:3]} == {"connection"}
    assert (
        sink.total(
            Metric.MODEL_FALLBACKS,
            from_provider=GEMINI,
            to_provider=OPENAI,
            reason_class="connection",
        )
        == 1
    )
    served = served_by(result)
    assert (served.provider, served.fallback_from, served.fallback_reason) == (  # type: ignore[attr-defined]
        OPENAI,
        GEMINI,
        "connection",
    )
