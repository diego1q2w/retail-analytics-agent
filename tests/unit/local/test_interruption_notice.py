"""What the user is told when a local investigation is interrupted."""

from __future__ import annotations

from retail_analytics.application.contracts.progress import ProgressUpdate
from retail_analytics.application.investigation_runtime import (
    INTERRUPTED_NOTICE,
    INTERRUPTED_SUMMARY,
    interruption_message_id,
    interruption_notice,
)
from retail_analytics.domain.investigations import answer_message_id


def test_notice_is_truthful_about_unsettled_work_and_queued_requests() -> None:
    assert interruption_notice(unsettled=0, queued=0) == INTERRUPTED_NOTICE
    unsettled = interruption_notice(unsettled=1, queued=0)
    assert "could not be confirmed stopped" in unsettled
    assert "cancelled" not in unsettled  # an uncertain job is never "cancelled"
    assert "Your queued request was not started" in interruption_notice(
        unsettled=0, queued=1
    )
    assert "Your 3 queued requests were not started" in interruption_notice(
        unsettled=0, queued=3
    )
    # Nothing claims the work resumed; the user restarts it explicitly.
    assert "not resumed" in INTERRUPTED_NOTICE
    assert "Send the request again" in INTERRUPTED_NOTICE


def test_notice_has_its_own_message_and_fits_a_progress_event() -> None:
    assert interruption_message_id("r1") != answer_message_id("r1", 0)
    assert interruption_message_id("r1") == interruption_message_id("r1")
    ProgressUpdate.model_validate(
        {
            "correlation": {"session_id": "s1", "run_id": "r1", "trace_id": "r1"},
            "kind": "run.failed",
            "summary": INTERRUPTED_SUMMARY,
        }
    )
