"""The human's explicit, typed confirmation of a report deletion.

The preview shown is the server's own record of the proposal (never the
assistant's chat text). Nothing is deleted unless the user types the exact
phrase for that count; anything else leaves every report untouched.
"""

from __future__ import annotations

from collections.abc import Callable

from retail_analytics.interfaces.cli.client import ApiClient, ApiError, Unreachable
from retail_analytics.interfaces.cli.render import (
    confirmation_phrase,
    format_deletion_preview,
    format_error,
    one_line,
)


def confirm_deletion(
    api: ApiClient,
    proposal_id: str,
    *,
    read_line: Callable[[str], str | None],
    out: Callable[[str], None],
) -> bool:
    """Show the exact proposal and delete only on the exact typed phrase."""
    preview = api.deletion_preview(proposal_id)
    out(format_deletion_preview(preview))
    if preview.get("status") != "pending":
        out(f"This proposal is {preview.get('status')}; nothing can be deleted by it.")
        return False
    phrase = confirmation_phrase(int(preview["count"]))
    typed = read_line(
        f'Type "{phrase}" to delete exactly these reports, or press Enter to keep '
        "them: "
    )
    if typed is None or typed.strip().lower() != phrase:
        out("Not confirmed. Nothing was deleted.")
        return False
    try:
        result = api.confirm_deletion(proposal_id)
    except Unreachable:
        out(
            "The connection was lost while confirming, so it is not known whether "
            "the reports were deleted. Check with: analytics deletion show "
            f"{one_line(proposal_id)}"
        )
        return False
    except ApiError as error:
        out(format_error(error))
        out("Nothing was deleted by this attempt.")
        return False
    out(
        f"Deleted {len(result['report_ids'])} report(s). An operator can restore "
        f"them until {one_line(result['recoverable_until'])}."
    )
    return True
