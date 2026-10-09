"""Sanitized interaction content: canaries per capture surface, bounds,
fail-closed omission, no hidden reasoning and the MLflow representation."""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from retail_analytics.adapters.telemetry.otel import OtelSink
from retail_analytics.application import telemetry_payloads
from retail_analytics.application.contracts.investigations import (
    AnswerDraft,
    QuestionDraft,
    StopReason,
)
from retail_analytics.application.contracts.telemetry import PayloadSide, Span
from retail_analytics.application.investigation_runtime import (
    _trace_answer,
    _trace_question,
)
from retail_analytics.application.investigations import _trace_input
from retail_analytics.application.output_privacy import OutputWithheld
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.application.telemetry_payloads import (
    MAX_PAYLOAD_CHARS,
    MAX_STRING_CHARS,
    capture,
)
from retail_analytics.domain.investigations import InputKind
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.models import stubs
from tests.unit.models.test_provider_chain import gemini_answer, gpt_answer, harness
from tests.unit.telemetry.recording import recording
from tests.unit.telemetry.test_otel_sink import settings

# Key-shaped canaries are built at runtime: no key-shaped literal in source.
GOOGLE_KEY = "AIza" + "SyCanary" + "0123456789" * 2
OPENAI_KEY = "sk-" + "proj-" + "canary" * 4 + "0123456789"
JWT_HEAD = "ey" + "JhbGciOiJIUzI1NiJ9"
BEARER = "Bearer " + JWT_HEAD + ".canary0123456789"
EMAIL = "maria.canary@example.com"
NAME = "Maria Lopez"
CUSTOMER = "customer_id = 48213"
CANARIES = (GOOGLE_KEY, OPENAI_KEY, JWT_HEAD, EMAIL, NAME, "48213")


def dumped(value: object) -> str:
    return json.dumps(capture(PayloadSide.INPUTS, value).content)


def assert_clean(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text, canary


@pytest.mark.parametrize(
    "text",
    [
        f"use key {GOOGLE_KEY} please",
        f"api_key={OPENAI_KEY}",
        f"Authorization: {BEARER}",
        f"send it to {EMAIL}",
        f"the customer named {NAME} bought most",
        f"filter {CUSTOMER} only",
        f"https://example.test/v1?key={GOOGLE_KEY}",
    ],
)
def test_free_text_canaries_are_masked_with_visible_markers(text: str) -> None:
    payload = capture(PayloadSide.INPUTS, {"text": text})
    assert_clean(json.dumps(payload.content))
    assert payload.redactions >= 1
    assert "[withheld]" in json.dumps(payload.content) or "[redacted]" in json.dumps(
        payload.content
    )


def test_secret_and_identity_fields_are_withheld_whatever_their_value() -> None:
    payload = capture(
        PayloadSide.INPUTS,
        {
            "password": "hunter2-plain",
            "api_key": "plain",
            "thought_signature": "opaque",
            "email": "x",
            "first_name": "Ann",
            "input_tokens": 12,
            "evidence_id": "ev_1",
        },
    )
    content = payload.content
    assert isinstance(content, dict)
    assert content["password"] == content["api_key"] == "[redacted]"
    assert content["thought_signature"] == "[redacted]"
    assert content["email"] == content["first_name"] == "[withheld]"
    assert content["input_tokens"] == 12 and content["evidence_id"] == "ev_1"
    assert "hunter2" not in json.dumps(content)


def test_sql_comments_identity_filters_and_unsafe_literals_are_removed() -> None:
    sql = (
        "SELECT SUM(sale_amount) AS revenue -- for maria.canary@example.com\n"
        "FROM sales_items /* secret note */ WHERE item_status = 'Complete' "
        "AND ordered_date >= '2024-01-01' AND city = 'Springfield' "
        f"AND last_name = 'Lopez' AND note = '{NAME}' AND user_id IN (48213, 5, 6)"
    )
    content = capture(
        PayloadSide.INPUTS,
        {"generated_sql": sql, "parameters": {"who": NAME, "status": "Shipped"}},
    ).content
    text = json.dumps(content)
    assert_clean(text)
    assert "secret note" not in text and "Lopez" not in text
    assert "Springfield" not in text
    assert "[comment removed]" in text
    # Dates and short codes stay readable.
    assert "'Complete'" in text and "'2024-01-01'" in text
    assert isinstance(content, dict)
    assert content["parameters"] == {"who": "[withheld]", "status": "Shipped"}


def test_exception_text_is_sanitized() -> None:
    error = RuntimeError(f"401 for {EMAIL} using key={GOOGLE_KEY} {BEARER}")
    text = dumped({"error": {"type": "RuntimeError", "message": str(error)}})
    assert_clean(text)
    assert "RuntimeError" in text


def test_oversized_payloads_are_cut_with_explicit_markers() -> None:
    payload = capture(
        PayloadSide.OUTPUTS,
        {"big": "a " * MAX_STRING_CHARS, "rows": list(range(500))},
    )
    text = json.dumps(payload.content)
    assert payload.truncated
    assert "[truncated:" in text and "more chars]" in text
    assert "more items]" in text
    many = capture(PayloadSide.OUTPUTS, ["b " * 9000 for _ in range(10)])
    assert many.truncated and many.chars <= MAX_PAYLOAD_CHARS * 1.25
    assert "more items]" in json.dumps(many.content)


def test_unsupported_values_and_failures_are_visibly_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Opaque:
        def __repr__(self) -> str:
            return EMAIL

    payload = capture(PayloadSide.INPUTS, {"thing": Opaque(), "n": float("nan")})
    assert EMAIL not in json.dumps(payload.content)
    assert set(payload.omitted) == {"unsupported_value", "non_finite_number"}

    def broken(text: str) -> tuple[str, int]:
        raise ValueError(text)

    monkeypatch.setattr(telemetry_payloads, "sanitize_text", broken)
    failed = capture(PayloadSide.INPUTS, {"text": EMAIL})
    assert failed.content == telemetry_payloads.FAILED
    assert failed.omitted == ("sanitization_failed",)


@pytest.mark.asyncio
async def test_model_attempts_capture_messages_tool_calls_and_no_signatures() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder(
        [
            stubs.Reply(
                events=stubs.gemini_call(
                    "execute_analysis", {"sql": f"SELECT 1 -- {EMAIL}"}
                )
            ),
            gemini_answer(f"Sales were 10. Ask {EMAIL}."),
        ]
    )
    with use_telemetry(telemetry):
        await harness(gemini, None).run(f"Sales last month? key {OPENAI_KEY}")
    first, second = sink.named(Span.MODEL_ATTEMPT)
    sent = first.content("inputs")
    assert isinstance(sent, dict)
    roles = [m["role"] for m in sent["messages"]]
    assert roles[0] == "system" and "user" in roles
    assert sent["messages"][0]["content"] == "Policy."
    assert sent["tools"] == ["execute_analysis"]
    got = first.content("outputs")
    assert isinstance(got, dict)
    (call,) = got["choices"][0]["message"]["tool_calls"]
    assert call["function"]["name"] == "execute_analysis"
    assert call["function"]["arguments"]["sql"].startswith("SELECT 1")
    assert got["omitted"] == {"provider_private_parts": 1}
    # The next attempt carries the tool result and the earlier tool call.
    later = second.content("inputs")
    assert isinstance(later, dict)
    tool_messages = [m for m in later["messages"] if m["role"] == "tool"]
    assert tool_messages and tool_messages[0]["content"] == {"evidence_id": "ev-1"}
    text = sink.everything()
    assert "Sales were 10" in text
    assert_clean(text)
    assert "thought-sig" not in text and "call-sig" not in text


@pytest.mark.asyncio
async def test_retries_and_fallback_keep_each_attempt_and_its_error() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder([stubs.error(503, "unavailable")] * 3)
    gpt = stubs.Recorder([gpt_answer()])
    with use_telemetry(telemetry):
        await harness(gemini, gpt).run()
    attempts = sink.named(Span.MODEL_ATTEMPT)
    assert len(attempts) == 4
    for failed in attempts[:3]:
        out = failed.content("outputs")
        assert isinstance(out, dict)
        assert out["error"]["status_code"] == 503
        assert out["error"]["reason_class"] == "server_error"
        sent = failed.content("inputs")
        assert isinstance(sent, dict) and sent["provider"] == "google-interactions"
    backup = attempts[3]
    sent = backup.content("inputs")
    assert isinstance(sent, dict) and sent["provider"] == "openai"
    assert [a.content("inputs")["attempt"] for a in attempts] == [1, 2, 3, 1]  # type: ignore[index]
    assert stubs.GEMINI_KEY not in sink.everything()
    assert stubs.OPENAI_KEY not in sink.everything()


def test_user_flow_distinguishes_drafts_released_and_withheld_outputs() -> None:
    telemetry, sink = recording()
    withheld = OutputWithheld(
        "personal_data",
        section="answer",
        code=ToolErrorCode.INVALID_INPUT,
        correctable=True,
    )
    with use_telemetry(telemetry):
        _trace_input("run-1", "in-1", InputKind.ANSWER, f"Use March, {EMAIL}", "q-1")
        _trace_question(
            QuestionDraft("run-1", 0, "Which month?"), released="Which month?"
        )
        _trace_answer(AnswerDraft("run-1", 1, f"Mail {EMAIL}"), withheld=withheld)
        _trace_answer(
            AnswerDraft("run-1", 2, "Sales were 10.", ("ev_1",)),
            released="Sales were 10.\nSource: ev_1",
        )
    (reply,) = sink.named(Span.USER_INPUT)
    assert reply.attributes["kind"] == "answer"
    assert reply.content("inputs") == {
        "kind": "answer",
        "question_id": "q-1",
        "text": "Use March, [withheld]",
    }
    (question,) = sink.named(Span.CLARIFICATION)
    assert question.content("outputs") == {
        "outcome": "asked",
        "released_question": "Which month?",
    }
    blocked, released = sink.named(Span.ANSWER)
    assert blocked.attributes["outcome"] == "withheld"
    assert blocked.content("outputs")["withheld_reason"] == "personal_data"  # type: ignore[index]
    assert released.content("inputs")["model_draft"] == "Sales were 10."  # type: ignore[index]
    assert released.content("outputs")["released_answer"].endswith("ev_1")  # type: ignore[index]
    assert EMAIL not in sink.everything()


def test_disabled_capture_records_no_content_but_keeps_metadata() -> None:
    telemetry, sink = recording(capture_content=False)
    with use_telemetry(telemetry):
        _trace_answer(AnswerDraft("run-1", 1, "Sales were 10."), released="Sales.")
    (span,) = sink.named(Span.ANSWER)
    assert span.payloads == []
    assert span.attributes["outcome"] == "released"
    assert span.attributes["content_capture"] == "disabled"


def test_mlflow_sees_payloads_as_span_inputs_and_outputs() -> None:
    exporter = InMemorySpanExporter()
    sink = OtelSink(settings(), span_exporter=exporter)
    sink._traces.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry(sink)
    with telemetry.span(Span.TOOL, run_id="run_1") as span:
        span.inputs({"arguments": {"purpose": f"check {EMAIL}"}})
        span.outputs({"model_visible_result": {"rows": list(range(300))}})
    exported = next(s for s in exporter.get_finished_spans() if s.name == Span.TOOL)
    sink.shutdown(1.0)
    attributes = dict(exported.attributes or {})
    inputs = json.loads(str(attributes["mlflow.spanInputs"]))
    assert inputs == {"arguments": {"purpose": "check [withheld]"}}
    assert json.loads(str(attributes["mlflow.spanOutputs"]))["model_visible_result"]
    assert attributes["capture.inputs.redactions"] == 1
    assert attributes["capture.outputs.truncated"] is True
    assert attributes["capture.inputs.chars"] == len(json.dumps(inputs))


@pytest.mark.asyncio
async def test_root_span_shows_request_answer_and_stop_reason() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from retail_analytics.application.contracts.investigations import FinishRequest
    from retail_analytics.application.investigation_runtime import (
        InvestigationRuntime,
    )
    from retail_analytics.domain.budgets import BudgetResource
    from retail_analytics.domain.investigations import InputStatus, RunInput
    from retail_analytics.domain.runs import Run, RunStatus

    now = datetime(2026, 10, 9, tzinfo=UTC)

    async def snapshot(run_id: str) -> None:
        return None

    async def for_run(run_id: str) -> list[RunInput]:
        return [
            RunInput(
                input_id="in-1",
                session_id="ses-1",
                kind=InputKind.REQUEST,
                content=f"Revenue by month? cc {EMAIL}",
                status=InputStatus.APPLIED,
                created_at=now,
                run_id="run-1",
            )
        ]

    runtime = SimpleNamespace(
        _clock=lambda: now,
        _budgets=SimpleNamespace(snapshot=snapshot),
        _inputs=SimpleNamespace(for_run=for_run),
    )
    run = Run("run-1", "ses-1", "exec-1", "m-1", "k-1", RunStatus.PARTIAL, now, now)
    telemetry, sink = recording()
    with use_telemetry(telemetry):
        await InvestigationRuntime._record_run_end(
            runtime,  # type: ignore[arg-type]
            run,
            None,
            "Stopped. Partial findings.",
            FinishRequest("run-1", StopReason.BUDGET, BudgetResource.TOKENS),
        )
    (root,) = sink.named(Span.RUN)
    assert root.content("inputs") == {"request": "Revenue by month? cc [withheld]"}
    assert root.content("outputs") == {
        "status": "partial",
        "stop_reason": StopReason.BUDGET.value,
        "stop_resource": BudgetResource.TOKENS.value,
        "released_answer": "Stopped. Partial findings.",
    }
