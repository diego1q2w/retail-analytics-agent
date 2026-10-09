"""Numbered citations and the Sources section in CLI answers."""

from __future__ import annotations

from typing import Any

import click
from click.testing import CliRunner

from retail_analytics.domain.citations import (
    citation_marks,
    first_use_order,
    number_citations,
    uses_numbered_prose,
)
from retail_analytics.interfaces.cli.app import cli
from retail_analytics.interfaces.cli.render import format_run_result, render_answer
from tests.unit.cli.fake_backend import Backend, run_view
from tests.unit.cli.test_chat import base, chat, finishing_stream

E1 = "evd_90c4aa0e60714f5883f527f2c355edf2"
E2 = "evd_0123456789abcdef0123456789abcdef"
FAKE = "evd_ffffffffffffffffffffffffffffffff"


def source(evidence_id: str, label: str, description: str, **kw: Any) -> Any:
    return {
        "number": kw.get("number", 1),
        "label": label,
        "evidence_id": evidence_id,
        "kind": "query",
        "description": description,
        "current": kw.get("current", True),
    }


SOURCES = [
    source(E1, "1", "Query result; September 2026 (UTC); computed 9 October 2026."),
    source(E2, "2", "Query result; period not recorded."),
]


# --- parser (shared domain) ---------------------------------------------------


def test_first_use_order_and_repeated_references() -> None:
    text = f"A [{E2}] then [{E1}, {E2}] and again {E2}."
    assert first_use_order(text) == (E2, E1)
    assert number_citations(text, {E2: "1", E1: "2"}) == (
        "A [1] then [2, 1] and again [1]."
    )


def test_code_urls_and_longer_words_are_never_rewritten() -> None:
    text = (
        f"`{E1}` and\n```\nselect '{E1}'\n```\n"
        f"https://example.com/{E1} [link](https://x.test/#{E1}) x{E1} {E1}_tail"
    )
    assert citation_marks(text) == ()
    assert number_citations(text, {E1: "1"}) == text


def test_unrecognized_ids_stay_as_written() -> None:
    text = f"Real [{E1}], invented [{FAKE}], mixed [{E1}, {FAKE}]."
    assert number_citations(text, {E1: "1"}) == (
        f"Real [1], invented [{FAKE}], mixed [1, {FAKE}]."
    )


def test_plain_application_forms() -> None:
    text = f"Evidence: {E1}, {E2}\n\nSources from saved reports:\n- {E1}: report"
    assert number_citations(text, {E1: "1", E2: "2"}) == (
        "Evidence: [1], [2]\n\nSources from saved reports:\n- [1]: report"
    )


def test_numbered_prose_is_detected_outside_code_only() -> None:
    assert uses_numbered_prose("See [1] and [2, 3].")
    assert not uses_numbered_prose("Code `a[1]` only")
    assert not uses_numbered_prose("```\nx[1]\n```")


# --- rendering ------------------------------------------------------------------


def test_answer_shows_numbers_and_one_source_per_record() -> None:
    answer = {
        "text": f"Revenue was 141,190.75 [{E1}]; again [{E1}]; orders [{E2}].",
        "withheld": False,
        "citations": SOURCES,
    }
    shown = click.unstyle(render_answer(answer))
    assert "Revenue was 141,190.75 [1]; again [1]; orders [2]." in shown
    assert "evd_" not in shown
    assert shown.count("[1] Query result; September 2026") == 1
    assert "SOURCES\n[1] " in shown
    # The server's text is not modified.
    assert E1 in str(answer["text"])


def test_malformed_or_missing_citations_never_break_rendering() -> None:
    text = f"Revenue [{E1}]."
    for citations in (None, "bad", [None, {"label": "1"}, {"evidence_id": E1}]):
        assert render_answer({"text": text, "citations": citations}) == text
    weird = [source(E1, "1; rm -rf", "x"), source("evd_X!", "2", "y")]
    assert render_answer({"text": text, "citations": weird}) == text
    blank = render_answer({"text": text, "citations": [source(E1, "1", "")]})
    assert "[1] Details of this result were not provided." in blank


def test_withheld_answer_ignores_citations() -> None:
    run = run_view("completed", answer="withheld text", withheld=True)
    run["answer"]["citations"] = SOURCES
    assert "SOURCES" not in format_run_result(run)


def test_partial_and_prefixed_labels() -> None:
    run = run_view(
        "partial",
        answer=f"Step [1] first. Evidence: {E1}",
        citations=[source(E1, "S1", "Query result.")],
    )
    shown = format_run_result(run)
    assert "PARTIAL RESULT" in shown
    assert "Step [1] first. Evidence: [S1]" in shown
    assert "[S1] Query result." in shown


def test_description_is_sanitized() -> None:
    shown = click.unstyle(
        render_answer(
            {
                "text": f"x [{E1}]",
                "citations": [source(E1, "1", "ok \x1b]0;evil\x07\nnext")],
            }
        )
    )
    assert "\x1b" not in shown and "\x07" not in shown and "evil" not in shown


# --- commands: show (reopen) and chat (fresh) --------------------------------------


def test_show_and_chat_use_the_same_mapping() -> None:
    text = f"Revenue was 10 [{E1}]."
    backend = Backend()
    backend.runs["r1"] = run_view("completed", answer=text, citations=SOURCES[:1])
    shown = CliRunner().invoke(cli, ["show", "r1"], obj=backend.client)
    assert shown.exit_code == 0, shown.output
    assert "Revenue was 10 [1]." in shown.output
    assert "[1] Query result; September 2026" in shown.output
    assert "\x1b" not in shown.output

    fresh = base()
    fresh.runs["r1"] = run_view("completed", answer=text, citations=SOURCES[:1])
    fresh.streams = [finishing_stream()]
    result = chat(fresh, "How much?\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "Revenue was 10 [1]." in result.output
    assert "[1] Query result; September 2026" in result.output

    raw = CliRunner().invoke(cli, ["show", "r1", "--json"], obj=backend.client)
    assert E1 in raw.output
