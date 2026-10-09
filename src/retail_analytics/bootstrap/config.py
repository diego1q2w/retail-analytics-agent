"""Typed configuration loaded once at the composition roots.

Backend settings use the ``RETAIL_ANALYTICS_`` prefix; CLI client settings use
``ANALYTICS_CLI_``. Values come from an optional dotenv file overlaid by the
process environment. Empty values count as unset.

The dotenv file is ``.env`` in the working directory unless
``RETAIL_ANALYTICS_ENV_FILE`` (in the process environment) names another file;
then that file is the only one read, and it must exist. Local bootstrap sets it
for every child command so an isolated ``--env-file`` never falls back to the
repository ``.env``. Precedence, highest first: process environment, the dotenv
file, defaults.

Validation errors name the offending variable and the problem, never the value,
so secrets cannot leak through error output. Inner layers never read the
environment: bootstrap passes the typed values they need.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

BACKEND_ENV_PREFIX = "RETAIL_ANALYTICS_"
CLI_ENV_PREFIX = "ANALYTICS_CLI_"
DEFAULT_ENV_FILE = Path(".env")
# Names the one dotenv file to read instead of ``.env`` in the working directory.
# It is a loader pointer, not a setting: it is never validated as one.
ENV_FILE_VARIABLE = BACKEND_ENV_PREFIX + "ENV_FILE"


class ConfigError(Exception):
    """Invalid or incomplete configuration. The message never contains values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n  " + "\n  ".join(problems))


class RuntimeMode(StrEnum):
    """``fixture``: offline deterministic fakes. ``live``: real services."""

    FIXTURE = "fixture"
    LIVE = "live"


_MIB = 1024 * 1024
_GIB = 1024 * _MIB

_STRICT_MODEL = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class BackendSettings(BaseModel):
    """Settings for the API and worker processes.

    Add new settings here (with a safe default or a live-mode requirement) rather
    than reading environment variables elsewhere.
    """

    model_config = _STRICT_MODEL

    mode: RuntimeMode = RuntimeMode.FIXTURE
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    artifact_dir: Path = Path("data/local/artifacts")
    # Per-artifact byte limits enforced before anything is stored.
    artifact_max_markdown_bytes: int = Field(
        default=1024 * 1024, ge=1024, le=16 * 1024 * 1024
    )
    artifact_max_binary_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024
    )

    database_url: SecretStr | None = None
    temporal_address: str | None = None
    temporal_namespace: str = "default"
    temporal_task_queue: str = "retail-analytics"
    bigquery_project: str | None = None
    bigquery_location: str = "US"
    # How long source schema metadata is trusted before it is re-read.
    schema_refresh_seconds: int = Field(default=3600, ge=60, le=86400)
    # Automatic reuse of evidence for current-data questions (design section 32).
    evidence_current_freshness_seconds: int = Field(default=900, ge=60, le=86400)
    # Mandatory audit events are deleted after this many days (design section 40).
    audit_retention_days: int = Field(default=90, ge=1, le=3650)
    # Most reports and expired investigations one cleanup run handles.
    cleanup_batch_size: int = Field(default=100, ge=1, le=10000)
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3-flash-preview"
    openai_api_key: SecretStr | None = None
    # Investigation agent models (live mode). Gemini is primary and is called
    # through the Interactions API; the OpenAI model (Responses API) is the
    # backup, enabled when RETAIL_ANALYTICS_OPENAI_API_KEY is set.
    agent_gemini_model: str = Field(default="gemini-3.8-flash", min_length=1)
    agent_openai_model: str = Field(default="gpt-5-mini", min_length=1)
    # Provider response limits: no first streamed token within this time
    # fails the request; afterwards only a stall between streamed events or
    # the per-request total does (a progressing response is not cut off).
    model_first_token_seconds: int = Field(default=60, ge=5, le=600)
    model_stream_stall_seconds: int = Field(default=30, ge=5, le=600)
    model_request_max_seconds: int = Field(default=180, ge=30, le=1800)
    # After the primary fails (attempts spent, long retry hint, rejected key
    # or model), requests skip it for this long and use the backup.
    model_primary_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    model_max_output_tokens: int = Field(default=8192, ge=256, le=65536)
    # Golden retrieval: "hashing" is the offline deterministic embedder.
    embedding_provider: Literal["hashing", "gemini"] = "hashing"
    embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = Field(default=768, ge=128, le=3072)
    # Exchange rates (ECB reference rates through Frankfurter; no API key).
    exchange_rate_base_url: str = "https://api.frankfurter.dev/v2"
    # Operator-declared currency of the dataset's amounts (ISO 4217). Declared,
    # never verified or inferred; unset means unknown and conversions refuse.
    source_currency_declared: str | None = None
    retrieval_max_results: int = Field(default=3, ge=1, le=3)
    retrieval_channel_candidates: int = Field(default=10, ge=3, le=100)
    # Unset thresholds resolve per embedding provider (see bootstrap.retrieval):
    # measured gemini values, or the offline hashing values.
    retrieval_min_similarity: float | None = Field(default=None, ge=-1.0, le=1.0)
    retrieval_min_lexical_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    # Weighted RRF: semantic channel weight (keyword = 1); 2.0 measured in T36-F1.
    retrieval_semantic_weight: float = Field(default=2.0, gt=0.0, le=100.0)
    # Run budgets (design section 39). Pinned per run when its accounting
    # opens; later changes only apply to new runs.
    run_active_seconds: int = Field(default=600, ge=30, le=86400)
    run_max_provider_requests: int = Field(default=20, ge=1, le=1000)
    run_max_tokens: int = Field(default=100_000, ge=1000, le=10_000_000)
    run_max_queries: int = Field(default=10, ge=1, le=1000)
    query_max_bytes: int = Field(default=_GIB, ge=10 * _MIB, le=1024 * _GIB)
    run_max_bytes: int = Field(default=5 * _GIB, ge=10 * _MIB, le=10240 * _GIB)
    query_max_corrections: int = Field(default=2, ge=0, le=10)
    # Attempts per operation that may end in a transient failure (total).
    max_transient_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=1.0, gt=0, le=60)
    retry_max_seconds: float = Field(default=20.0, gt=0, le=600)
    # Warehouse query deadline, then cancel and reconcile.
    query_deadline_seconds: int = Field(default=120, ge=10, le=3600)
    # Per tool result, whichever is reached first; truncation is flagged.
    result_max_rows: int = Field(default=500, ge=1, le=10_000)
    result_max_bytes: int = Field(default=256 * 1024, ge=1024, le=16 * _MIB)
    # Local (simulated) token authentication; see README "Authentication".
    auth_issuer: str = Field(default="retail-analytics-local", min_length=1)
    auth_audience: str = Field(default="retail-analytics-api", min_length=1)
    auth_signing_key: SecretStr | None = None
    # Telemetry (design section 20): MLflow traces over OTLP/HTTP protobuf and
    # Prometheus metrics through its OTLP receiver. Best effort: bounded
    # queues and short timeouts; a backend outage drops telemetry only.
    telemetry_enabled: bool = False
    telemetry_traces_endpoint: str = "http://127.0.0.1:55500/v1/traces"
    telemetry_metrics_endpoint: str = "http://127.0.0.1:59090/api/v1/otlp/v1/metrics"
    telemetry_experiment_id: str = Field(default="0", pattern=r"^[0-9]{1,18}$")
    telemetry_export_timeout_seconds: float = Field(default=2.0, ge=0.5, le=10.0)
    telemetry_metric_interval_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    # Master key for opaque customer/order/item references. Unset: references
    # are unavailable and queries needing them fail closed.
    reference_key: SecretStr | None = None

    @field_validator("source_currency_declared")
    @classmethod
    def _check_declared_currency(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("must be a three-letter uppercase ISO 4217 code")
        return value

    @model_validator(mode="after")
    def _check_key_lengths(self) -> Self:
        for name in ("auth_signing_key", "reference_key"):
            key = getattr(self, name)
            if key is not None and len(key.get_secret_value().encode()) < 32:
                raise ValueError(
                    BACKEND_ENV_PREFIX + name.upper() + " must be at least 32 bytes"
                )
        return self

    @model_validator(mode="after")
    def _check_budget_order(self) -> Self:
        if self.query_max_bytes > self.run_max_bytes:
            raise ValueError(
                "RETAIL_ANALYTICS_QUERY_MAX_BYTES must not exceed "
                "RETAIL_ANALYTICS_RUN_MAX_BYTES"
            )
        if self.model_first_token_seconds > self.model_request_max_seconds:
            raise ValueError(
                "RETAIL_ANALYTICS_MODEL_FIRST_TOKEN_SECONDS must not exceed "
                "RETAIL_ANALYTICS_MODEL_REQUEST_MAX_SECONDS"
            )
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError(
                "RETAIL_ANALYTICS_RETRY_BASE_SECONDS must not exceed "
                "RETAIL_ANALYTICS_RETRY_MAX_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _require_live_settings(self) -> Self:
        if self.mode is RuntimeMode.LIVE:
            missing = [
                BACKEND_ENV_PREFIX + name.upper()
                for name in LIVE_REQUIRED_SETTINGS
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError("live mode requires " + ", ".join(missing))
        return self

    def redacted_summary(self) -> dict[str, str]:
        """Printable view: secrets reduced to set/unset."""
        summary: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                shown = "<set>"
            elif value is None:
                shown = "<unset>"
            else:
                shown = str(value)
            summary[BACKEND_ENV_PREFIX + name.upper()] = shown
        return summary


# OpenAI is the optional fallback provider, so it is not required in live mode.
# The signing key is required: every API route authenticates a bearer token.
LIVE_REQUIRED_SETTINGS: tuple[str, ...] = (
    "database_url",
    "temporal_address",
    "bigquery_project",
    "gemini_api_key",
    "auth_signing_key",
)


class CliSettings(BaseModel):
    """Settings for the ``analytics`` client. It holds no backend credentials."""

    model_config = _STRICT_MODEL

    api_url: str = "http://127.0.0.1:8080"
    timeout_seconds: float = Field(default=10.0, gt=0)
    # The bearer token (never printed) or a file holding it; the file wins when
    # both are set so a rotated token is picked up without editing the shell.
    token: SecretStr | None = None
    token_file: Path | None = None


def load_backend_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> BackendSettings:
    return _validate(BackendSettings, BACKEND_ENV_PREFIX, environ, env_file)


def load_cli_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> CliSettings:
    return _validate(CliSettings, CLI_ENV_PREFIX, environ, env_file)


def _validate[M: BaseModel](
    model: type[M],
    prefix: str,
    environ: Mapping[str, str] | None,
    env_file: Path | None,
) -> M:
    raw = _collect(prefix, environ, env_file)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_describe(exc, prefix)) from None


def _collect(
    prefix: str, environ: Mapping[str, str] | None, env_file: Path | None
) -> dict[str, str]:
    merged: dict[str, str] = {}
    process = os.environ if environ is None else environ
    pointed = process.get(ENV_FILE_VARIABLE, "")
    if pointed != "" and env_file == DEFAULT_ENV_FILE:
        env_file = Path(pointed)
        if not env_file.is_file():
            raise ConfigError([f"{ENV_FILE_VARIABLE}: file does not exist"])
    if env_file is not None and env_file.is_file():
        merged.update(
            {k: v for k, v in dotenv_values(env_file).items() if v is not None}
        )
    merged.update(process)
    return {
        key.removeprefix(prefix).lower(): value
        for key, value in merged.items()
        if key.startswith(prefix) and key != ENV_FILE_VARIABLE and value != ""
    }


def _describe(exc: ValidationError, prefix: str) -> list[str]:
    problems = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        name = prefix + location.upper() if location else "configuration"
        problems.append(f"{name}: {error['msg']}")
    return problems
