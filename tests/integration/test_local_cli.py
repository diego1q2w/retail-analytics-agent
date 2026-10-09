"""The ``analytics`` CLI tests of ``test_cli`` with local execution (default).

PostgreSQL only; the API process runs the investigations. Covers, unchanged:
reconnecting mid-run with Last-Event-ID, a new CLI process attaching while a
disconnect leaves the run going, answering a clarification, cancel, the typed
deletion confirmation (and declining it), pending proposals, a scripted chat
that saves and reads a report and deletes it, and Ctrl-C detaching.
"""

from __future__ import annotations

import pytest

from retail_analytics.domain.runs import ExecutionBackend
from tests.integration.test_cli import (  # noqa: F401  (fixtures reused)
    alice,
    test_chat_surfaces_a_proposal_without_parsing_answer_text,
    test_cli_answers_a_clarification_question,
    test_cli_cancel_stops_the_run_and_reports_its_state,
    test_cli_lists_only_own_pending_unexpired_proposals,
    test_cli_numbers_citations_on_display_and_reopening,
    test_cli_reconnects_mid_run_with_last_event_id_without_duplicates,
    test_deletion_needs_typed_phrase_and_declined_confirmation_deletes_nothing,
    test_new_cli_process_attaches_to_in_progress_run_and_disconnect_keeps_it,
    test_scripted_chat_session_ask_follow_up_report_and_delete,
    test_terminal_acknowledges_first_queued_and_follow_up_messages,
    test_terminal_session_steers_then_ctrl_c_detaches_without_cancelling,
)
from tests.integration.test_http_api import api, world  # noqa: F401

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def backend() -> ExecutionBackend:
    return ExecutionBackend.LOCAL
