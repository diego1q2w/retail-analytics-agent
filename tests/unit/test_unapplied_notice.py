from __future__ import annotations

from datetime import UTC, datetime

from retail_analytics.domain.investigations import (
    InputKind,
    InputStatus,
    RunInput,
    unapplied_notice,
    unapplied_summary,
)


def _steering(content: str) -> RunInput:
    return RunInput(
        input_id="inp-" + content[:8],
        session_id="s1",
        kind=InputKind.STEERING,
        content=content,
        status=InputStatus.PENDING,
        created_at=datetime(2026, 10, 9, tzinfo=UTC),
        run_id="r1",
    )


def test_notice_quotes_the_message_and_never_claims_it_was_used() -> None:
    notice = unapplied_notice([_steering("Instead use annual sales.")])
    assert '"Instead use annual sales."' in notice
    assert "was not applied to this answer" in notice
    assert "Send it again" in notice


def test_notice_names_every_message_and_shortens_long_ones() -> None:
    long = "word " * 100
    notice = unapplied_notice([_steering("only women"), _steering(long)])
    assert notice.startswith("Note: your 2 messages")
    assert '"only women"' in notice
    assert "…" in notice and len(notice) < 400


def test_event_summary_fits_and_carries_no_user_text() -> None:
    for count in (1, 3):
        summary = unapplied_summary(count)
        assert 0 < len(summary) <= 280
        assert "not applied" in summary
