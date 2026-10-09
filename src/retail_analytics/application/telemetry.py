"""Sanitizing, never-failing front door for traces and metrics.

Application code calls :func:`telemetry` and never touches an exporter. Every
value passes through this module before a sink sees it:

- attribute keys naming prompts, SQL, rows, tokens, secrets and the like are
  dropped (``input_tokens`` counts are fine: whole words are matched);
- identifiers must be identifier-shaped; other strings are dropped when they
  look like data dumps (JSON, quotes, ``=``, SQL punctuation), else masked
  with ``redact_for_telemetry`` and secret-shaped runs, and truncated. Free
  text is for application-authored codes and messages only: personal names
  in arbitrary prose are not detectable, so never pass user or row text;
- metric labels are limited to :class:`Label` keys with short code-like values,
  so run, session, operation or trace ids can never become label values;
- a failing sink never raises into the caller and never delays it: exporters
  are bounded and drop data (see ``adapters.telemetry``).

The holder is process-wide on purpose (as OpenTelemetry's own providers are):
bootstrap installs the real sink once; tests install a recording sink with
:func:`use_telemetry`.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import datetime
from types import TracebackType

from retail_analytics.application.contracts.telemetry import (
    Attributes,
    AttributeValue,
    Label,
    Metric,
    ProviderAttribution,
    ReasonClass,
    Span,
)
from retail_analytics.application.ports.telemetry import SpanHandle, TelemetrySink

MAX_TEXT = 120
MAX_ATTRIBUTES = 40
_KEY = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.:/{}+-]{1,64}$")
# Whole words that mark a key as carrying content or credentials.
_DENIED_WORDS = frozenset(
    {
        "prompt",
        "sql",
        "statement",
        "row",
        "rows",
        "token",
        "secret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "apikey",
        "authorization",
        "cookie",
        "email",
        "text",
        "content",
        "body",
        "parameters",
        "params",
        "reasoning",
        "answer",
        "question",
        "message",
        "key",
        "name",
        "address",
        "phone",
    }
)
_SECRET_SHAPES = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bAIza[0-9A-Za-z_-]{10,}|\beyJ[A-Za-z0-9_-]{8,}"
    r"|\bBearer\s+\S+|\b(?:ghp|gho|xox[bap])[-_][A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]+-----)",
    re.IGNORECASE,
)
_LONG_OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
# Values that look like data dumps (JSON, row reprs, SQL fragments, shell) are
# dropped outright instead of masked.
_STRUCTURED = re.compile(r"[{}\[\]\"`=;<>|\\]|'.*'")
_MASK = "[redacted]"
_OTHER = "other"


def trace_id_for(run_id: str) -> str:
    """The 32-hex trace id every component derives from a run id.

    API, workflow, activities, tool calls and warehouse jobs of one run share
    one trace without passing trace context across process boundaries.
    """
    return hashlib.sha256(b"trace:" + run_id.encode()).hexdigest()[:32]


def root_span_id_for(run_id: str) -> str:
    """The 16-hex id of the run's root span (emitted when the run closes)."""
    return hashlib.sha256(b"root:" + run_id.encode()).hexdigest()[:16]


ATTRIBUTION_METADATA_KEY = "retail_attribution"


def attribution_to_metadata(value: ProviderAttribution) -> dict[str, str | int]:
    data: dict[str, str | int] = {
        "provider": value.provider,
        "model": value.model,
        "attempt": value.attempt,
    }
    if value.fallback_from is not None:
        data["fallback_from"] = value.fallback_from
    if value.fallback_reason is not None:
        data["fallback_reason"] = value.fallback_reason
    return data


def attribution_from_metadata(value: object) -> ProviderAttribution | None:
    if not isinstance(value, dict):
        return None
    provider, model, attempt = (
        value.get("provider"),
        value.get("model"),
        value.get("attempt"),
    )
    if not (
        isinstance(provider, str)
        and isinstance(model, str)
        and isinstance(attempt, int)
        and not isinstance(attempt, bool)
    ):
        return None
    origin, reason = value.get("fallback_from"), value.get("fallback_reason")
    return ProviderAttribution(
        provider,
        model,
        attempt,
        origin if isinstance(origin, str) else None,
        reason if isinstance(reason, str) else None,
    )


# -- sanitizing -------------------------------------------------------------


def _scrub(text: str) -> str:
    # Imported here: the output gate itself reports to this module.
    from retail_analytics.application.output_privacy import redact_for_telemetry

    masked = redact_for_telemetry(text)
    masked = _SECRET_SHAPES.sub(_MASK, masked)
    masked = _LONG_OPAQUE.sub(_MASK, masked)
    return masked[:MAX_TEXT]


def _key_allowed(key: str) -> bool:
    if not _KEY.fullmatch(key):
        return False
    return not any(word in _DENIED_WORDS for word in re.split(r"[._]", key))


def _is_id_key(key: str) -> bool:
    return key.endswith("_id") or key.endswith(".id")


def _clean_value(key: str, value: object) -> AttributeValue | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    if _is_id_key(key):
        if _ID.fullmatch(value) and not _SECRET_SHAPES.search(value):
            return value
        return None
    if _STRUCTURED.search(value):
        return None
    return _scrub(value)


def sanitize_attributes(attributes: Mapping[str, object] | None) -> Attributes:
    clean: Attributes = {}
    for key, value in (attributes or {}).items():
        if len(clean) >= MAX_ATTRIBUTES:
            break
        if not _key_allowed(key):
            continue
        cleaned = _clean_value(key, value)
        if cleaned is not None:
            clean[key] = cleaned
    return clean


def sanitize_labels(labels: Mapping[Label, object] | None) -> dict[Label, str]:
    clean: dict[Label, str] = {}
    for key, value in (labels or {}).items():
        text = str(value)
        if (
            not _LABEL_VALUE.fullmatch(text)
            or _SECRET_SHAPES.search(text)
            or _LONG_OPAQUE.fullmatch(text)
        ):
            text = _OTHER
        clean[key] = text
    return clean


# -- spans ------------------------------------------------------------------


class _NoSpan:
    def set(self, attributes: Attributes) -> None:
        return None

    def event(self, name: str, attributes: Attributes) -> None:
        return None

    def fail(self, error_type: str) -> None:
        return None


_NO_SPAN = _NoSpan()


class SpanRecorder:
    """Sanitizes what callers record and swallows sink errors."""

    def __init__(self, inner: SpanHandle) -> None:
        self._inner = inner

    def set(self, attributes: Mapping[str, object]) -> None:
        with suppress(Exception):
            self._inner.set(sanitize_attributes(attributes))

    def event(self, name: str, attributes: Mapping[str, object] | None = None) -> None:
        with suppress(Exception):
            if _KEY.fullmatch(name):
                self._inner.event(name, sanitize_attributes(attributes))

    def fail(self, error_type: str) -> None:
        with suppress(Exception):
            self._inner.fail(_scrub(error_type)[:64])


class _SafeSpan:
    def __init__(
        self,
        sink: TelemetrySink | None,
        name: str,
        run_id: str | None,
        attributes: Mapping[str, object] | None,
        start: datetime | None,
        root: bool,
    ) -> None:
        self._sink = sink
        self._name = name
        self._run_id = run_id
        self._attributes = attributes
        self._start = start
        self._root = root
        self._context: AbstractContextManager[SpanHandle] | None = None
        self.handle = SpanRecorder(_NO_SPAN)

    def __enter__(self) -> SpanRecorder:
        if self._sink is None:
            return self.handle
        try:
            run_id = self._run_id
            if run_id is not None and not _ID.fullmatch(run_id):
                run_id = None
            context = self._sink.span(
                self._name,
                run_id=run_id,
                attributes=sanitize_attributes(self._attributes),
                start=self._start,
                root=self._root,
            )
            self.handle = SpanRecorder(context.__enter__())
            self._context = context
        except Exception:
            self._context = None
            self.handle = SpanRecorder(_NO_SPAN)
        return self.handle

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._context is None:
            return None
        try:
            if exc_type is not None:
                # The class name only: messages may carry data values.
                self.handle.fail(exc_type.__name__)
            self._context.__exit__(None, None, None)
        except Exception:  # noqa: S110
            pass
        return None


class Telemetry:
    """The application's telemetry facade (no-op without a sink)."""

    def __init__(self, sink: TelemetrySink | None = None) -> None:
        self._sink = sink

    @property
    def enabled(self) -> bool:
        return self._sink is not None

    def span(
        self,
        name: Span | str,
        *,
        run_id: str | None = None,
        attributes: Mapping[str, object] | None = None,
        start: datetime | None = None,
        root: bool = False,
    ) -> AbstractContextManager[SpanRecorder]:
        return _SafeSpan(self._sink, str(name), run_id, attributes, start, root)

    def count(
        self,
        metric: Metric,
        labels: Mapping[Label, object] | None = None,
        value: float = 1.0,
    ) -> None:
        if self._sink is None:
            return
        with suppress(Exception):
            self._sink.count(metric, value, sanitize_labels(labels))

    def observe(
        self,
        metric: Metric,
        value: float,
        labels: Mapping[Label, object] | None = None,
    ) -> None:
        if self._sink is None or not math.isfinite(value):
            return
        with suppress(Exception):
            self._sink.observe(metric, value, sanitize_labels(labels))

    def flush(self, timeout_seconds: float = 2.0) -> None:
        if self._sink is None:
            return
        with suppress(Exception):
            self._sink.flush(timeout_seconds)


_current = Telemetry()


def telemetry() -> Telemetry:
    return _current


def install_telemetry(value: Telemetry) -> None:
    global _current
    _current = value


@contextmanager
def use_telemetry(value: Telemetry) -> Iterator[Telemetry]:
    """Install ``value`` for a block (tests), then restore the previous one."""
    global _current
    previous = _current
    _current = value
    try:
        yield value
    finally:
        _current = previous


class Stopwatch:
    """Monotonic elapsed seconds for duration histograms."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    def seconds(self) -> float:
        return time.monotonic() - self._started


def classify_reason_text(status: int | None, *, timeout: bool = False) -> ReasonClass:
    """Coarse failure class from an HTTP status (or a timeout)."""
    if timeout:
        return ReasonClass.TIMEOUT
    if status is None:
        return ReasonClass.CONNECTION
    if status == 429:
        return ReasonClass.RATE_LIMITED
    if status in (401, 403, 404):
        return ReasonClass.REJECTED
    if status in (408, 504):
        return ReasonClass.TIMEOUT
    if status >= 500:
        return ReasonClass.SERVER_ERROR
    return ReasonClass.OTHER
