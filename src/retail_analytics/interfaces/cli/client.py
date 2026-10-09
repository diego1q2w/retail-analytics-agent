"""Typed access to the backend's HTTP API for the CLI.

Every write carries a ``submission_key``; a request that fails with a transport
error or 503 is resent with the same key, so a lost response never starts a
second run. Errors become :class:`ApiError` from the ``{"error": {...}}``
envelope. The bearer token lives only in the ``httpx.Client`` headers.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

JsonObject = dict[str, Any]

MISSING_CREDENTIAL_HELP = (
    "no access token: set ANALYTICS_CLI_TOKEN or ANALYTICS_CLI_TOKEN_FILE "
    "(local development: retail-analytics-dev-access token demo-a)"
)


class ApiError(Exception):
    """A structured error answered by the backend (or the CLI's own precheck)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 0,
        details: JsonObject | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        super().__init__(f"{code}: {message}")


class Unreachable(Exception):
    """The backend could not be reached (after any resends)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def new_submission_key() -> str:
    return "cli-" + uuid.uuid4().hex


def error_from_response(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
        error = body["error"]
        return ApiError(
            str(error["code"]),
            str(error["message"]),
            status=response.status_code,
            details=error.get("details") or {},
        )
    except (ValueError, KeyError, TypeError):
        return ApiError(
            "unexpected_response",
            f"the backend answered HTTP {response.status_code} without an error body",
            status=response.status_code,
        )


class ApiClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        resends: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http
        self._resends = resends
        self._sleep = sleep

    def require_token(self) -> None:
        if "authorization" not in self.http.headers:
            raise ApiError("no_token", MISSING_CREDENTIAL_HELP)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: JsonObject | None = None,
        params: dict[str, Any] | None = None,
        resend: bool = True,
    ) -> httpx.Response:
        self.require_token()
        attempt = 0
        while True:
            try:
                response = self.http.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                if resend and attempt < self._resends:
                    attempt += 1
                    self._sleep(min(0.5 * 2**attempt, 4.0))
                    continue
                raise Unreachable(type(exc).__name__) from None
            if response.status_code == 503 and resend and attempt < self._resends:
                attempt += 1
                self._sleep(min(0.5 * 2**attempt, 4.0))
                continue
            if response.status_code >= 400:
                raise error_from_response(response)
            return response

    def _json(self, method: str, path: str, **kwargs: Any) -> JsonObject:
        body = self.request(method, path, **kwargs).json()
        if not isinstance(body, dict):
            raise ApiError("unexpected_response", "the backend answered a non-object")
        return body

    # --- sessions and runs -------------------------------------------------------

    def open_session(self, key: str) -> JsonObject:
        return self._json("POST", "/v1/sessions", json={"submission_key": key})

    def list_sessions(self, limit: int = 20) -> JsonObject:
        return self._json("GET", "/v1/sessions", params={"limit": limit})

    def get_session(self, session_id: str) -> JsonObject:
        return self._json("GET", f"/v1/sessions/{quote(session_id)}")

    def start_run(self, session_id: str, text: str, key: str) -> JsonObject:
        return self._json(
            "POST",
            f"/v1/sessions/{quote(session_id)}/runs",
            json={"text": text, "submission_key": key},
        )

    def send_message(
        self, session_id: str, text: str, key: str, *, mode: str = "steer"
    ) -> JsonObject:
        return self._json(
            "POST",
            f"/v1/sessions/{quote(session_id)}/messages",
            json={"text": text, "submission_key": key, "mode": mode},
        )

    def get_run(self, run_id: str) -> JsonObject:
        return self._json("GET", f"/v1/runs/{quote(run_id)}")

    def steer(self, run_id: str, text: str, key: str) -> JsonObject:
        return self._json(
            "POST",
            f"/v1/runs/{quote(run_id)}/steer",
            json={"text": text, "submission_key": key},
        )

    def answer(self, run_id: str, question_id: str, text: str, key: str) -> JsonObject:
        return self._json(
            "POST",
            f"/v1/runs/{quote(run_id)}/answers",
            json={"question_id": question_id, "text": text, "submission_key": key},
        )

    def cancel(self, run_id: str) -> JsonObject:
        # Cancelling twice is harmless, so a lost response may be resent.
        return self._json("POST", f"/v1/runs/{quote(run_id)}/cancel")

    # --- reports -------------------------------------------------------------------

    def list_reports(self, *, session_id: str | None = None, limit: int = 50) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if session_id:
            params["session_id"] = session_id
        return self._json("GET", "/v1/reports", params=params)

    def search_reports(self, query: str, *, limit: int = 25) -> JsonObject:
        return self._json(
            "GET", "/v1/reports/search", params={"q": query, "limit": limit}
        )

    def get_report(self, report_id: str, version: int | None = None) -> JsonObject:
        params = None if version is None else {"version": version}
        return self._json("GET", f"/v1/reports/{quote(report_id)}", params=params)

    def report_versions(self, report_id: str) -> JsonObject:
        return self._json("GET", f"/v1/reports/{quote(report_id)}/versions")

    def export_report(self, report_id: str) -> tuple[str, bytes]:
        response = self.request("GET", f"/v1/reports/{quote(report_id)}/export")
        disposition = response.headers.get("content-disposition", "")
        name = f"{report_id}.md"
        if 'filename="' in disposition:
            name = disposition.split('filename="', 1)[1].split('"', 1)[0] or name
        return name, response.content

    # --- deletion --------------------------------------------------------------------

    def deletion_preview(self, proposal_id: str) -> JsonObject:
        return self._json("GET", f"/v1/deletion-proposals/{quote(proposal_id)}")

    def confirm_deletion(self, proposal_id: str) -> JsonObject:
        # Resent only on 503 (nothing applied); a transport failure is reported
        # as unknown instead, because the deletion may have happened.
        return self._json(
            "POST",
            f"/v1/deletion-proposals/{quote(proposal_id)}/confirm",
            json={"confirm": True},
            resend=False,
        )

    def cancel_deletion(self, proposal_id: str) -> JsonObject:
        return self._json("POST", f"/v1/deletion-proposals/{quote(proposal_id)}/cancel")
