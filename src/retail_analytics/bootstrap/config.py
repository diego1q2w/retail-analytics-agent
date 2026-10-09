"""Typed configuration loaded once at the composition roots.

Each backend setting is read from its own variable: the field name upper-cased
(``GEMINI_API_KEY``, ``EXECUTION_BACKEND``), except where a bare name is too
generic for a shell (``APP_MODE``, ``APP_DATABASE_URL``, ``APP_API_HOST``,
``APP_API_PORT``; README,
"Environment variables", has the full list). CLI client
settings use ``CLI_`` (``CLI_API_URL``, ``CLI_TOKEN``). Values come from an
optional dotenv file overlaid by the process environment; the process
environment is read only for these known names. Empty values count as unset.

The dotenv file may hold only known names and ``COMPOSE_*`` keys (read by
Docker Compose): any other key is reported as a probable typo. The older
prefixed names (``RETAIL_ANALYTICS_*``, ``ANALYTICS_CLI_*``) are refused wherever they
appear, with the fix (``./scripts/bootstrap.sh --env-only`` migrates the file);
there is no silent dual support.

The dotenv file is ``.env`` in the working directory unless
``APP_ENV_FILE`` (in the process environment) names another file;
then that file is the only one read, and it must exist. Local bootstrap sets it
for every child command so an isolated ``--env-file`` never falls back to the
repository ``.env``. Precedence, highest first: process environment, the dotenv
file, defaults.

Validation errors name the offending variable and the problem, never the value,
so secrets cannot leak through error output. Inner layers never read the
environment: bootstrap passes the typed values they need.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections.abc import Mapping
from decimal import Decimal
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
    ValidationInfo,
    field_validator,
    model_validator,
)

from retail_analytics.domain.runs import ExecutionBackend

CLI_ENV_PREFIX = "CLI_"
# Keys for Docker Compose interpolation; allowed in the dotenv file, never read.
COMPOSE_ENV_PREFIX = "COMPOSE_"
DEFAULT_ENV_FILE = Path(".env")
# Names the one dotenv file to read instead of ``.env`` in the working directory.
# It is a loader pointer, not a setting: it is never validated as one.
ENV_FILE_VARIABLE = "APP_ENV_FILE"
# Backend fields whose bare upper-case name is too generic for a shell.
_BACKEND_NAME_OVERRIDES: dict[str, str] = {
    "mode": "APP_MODE",
    "database_url": "APP_DATABASE_URL",
    "api_host": "APP_API_HOST",
    "api_port": "APP_API_PORT",
}
# The older prefixed names. Refused by the loader; migrated by
# ``./scripts/bootstrap.sh --env-only`` (bootstrap.local_env.migrate_legacy).
LEGACY_BACKEND_PREFIX = "RETAIL_ANALYTICS_"
LEGACY_CLI_PREFIX = "ANALYTICS_CLI_"
LEGACY_PREFIXES = (LEGACY_BACKEND_PREFIX, LEGACY_CLI_PREFIX)
MIGRATE_COMMAND = "./scripts/bootstrap.sh --env-only"


def backend_env_name(field: str) -> str:
    """The environment variable of a ``BackendSettings`` field."""
    return _BACKEND_NAME_OVERRIDES.get(field, field.upper())


def cli_env_name(field: str) -> str:
    """The environment variable of a ``CliSettings`` field."""
    return CLI_ENV_PREFIX + field.upper()


class ConfigError(Exception):
    """Invalid or incomplete configuration. The message never contains values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n  " + "\n  ".join(problems))


class RuntimeMode(StrEnum):
    """``fixture``: offline deterministic fakes. ``live``: real services."""

    FIXTURE = "fixture"
    LIVE = "live"


class ModelPriceSetting(BaseModel):
    """One model's price override, USD per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: Decimal = Field(ge=0, le=10_000)
    output: Decimal = Field(ge=0, le=10_000)
    # Unset: cached input is priced as ordinary input.
    cached_input: Decimal | None = Field(default=None, ge=0, le=10_000)


_MIB = 1024 * 1024
_GIB = 1024 * _MIB

_STRICT_MODEL = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class BackendSettings(BaseModel):
    """Settings for the API and worker processes.

    Add new settings here (with a safe default or a live-mode requirement) rather
    than reading environment variables elsewhere.
    """

    model_config = _STRICT_MODEL

    mode: RuntimeMode = RuntimeMode.LIVE
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
    # Where investigations execute, independent of ``mode``. ``local`` (the
    # default): tasks of the API process, PostgreSQL only, interrupted if the
    # API stops. ``temporal`` (opt-in): durable workflows run by
    # ``retail-analytics-worker``; needs the Temporal settings below.
    execution_backend: ExecutionBackend = ExecutionBackend.LOCAL
    # Local backend: investigations executing at once in the API process, and
    # how long shutdown waits for them before marking them interrupted.
    local_max_concurrent_runs: int = Field(default=4, ge=1, le=64)
    local_shutdown_grace_seconds: float = Field(default=10.0, ge=0.0, le=300.0)
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
    # backup, enabled when OPENAI_API_KEY is set.
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
    # Prices replacing the maintained price list (genai-prices) for exact
    # model names, USD per million tokens, as JSON:
    # {"<model>": {"input": 0.75, "cached_input": 0.075, "output": 3.75}}.
    # Needed for a model the list does not know while a dollar limit applies.
    model_price_overrides: dict[str, ModelPriceSetting] = Field(default_factory=dict)
    # Golden retrieval: "hashing" is the offline deterministic embedder. Unset:
    # gemini in live mode (the provider the thresholds were measured with),
    # hashing in fixture mode. Live mode never substitutes hashing on its own.
    embedding_provider: Literal["hashing", "gemini"] = Field(
        default=None,
        validate_default=True,
    )
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
    # Hard limit on a run's active work (clarification waits excluded).
    run_active_seconds: int = Field(default=120, ge=30, le=86400)
    run_max_provider_requests: int = Field(default=20, ge=1, le=1000)
    run_max_tokens: int = Field(default=100_000, ge=1000, le=10_000_000)
    run_max_queries: int = Field(default=10, ge=1, le=1000)
    query_max_bytes: int = Field(default=_GIB, ge=10 * _MIB, le=1024 * _GIB)
    run_max_bytes: int = Field(default=5 * _GIB, ge=10 * _MIB, le=10240 * _GIB)
    query_max_corrections: int = Field(default=2, ge=0, le=10)
    # Soft limit on estimated model spend per investigation (USD; every
    # provider attempt, retry and fallback). Checked before each request, so
    # the request that crosses it may overshoot. 0 turns the dollar limit off
    # (the token and request limits still apply).
    run_max_model_cost_usd: Decimal = Field(
        default=Decimal("1"), ge=0, le=1000, max_digits=12, decimal_places=6
    )
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
    # queues and short timeouts; a backend outage drops telemetry only. On by
    # default for local runs (the endpoints are the local compose services);
    # tests and offline checks switch it off explicitly (tests/conftest.py).
    telemetry_enabled: bool = True
    telemetry_traces_endpoint: str = "http://127.0.0.1:55500/v1/traces"
    telemetry_metrics_endpoint: str = "http://127.0.0.1:59090/api/v1/otlp/v1/metrics"
    telemetry_experiment_id: str = Field(default="0", pattern=r"^[0-9]{1,18}$")
    telemetry_export_timeout_seconds: float = Field(default=2.0, ge=0.5, le=10.0)
    telemetry_metric_interval_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    # Sanitized model/tool/user interaction content in traces (docs/
    # observability.md). Off keeps timings, ids, outcomes and token counts.
    telemetry_capture_content: bool = True
    # Master key for opaque customer/order/item references. Unset: references
    # are unavailable and queries needing them fail closed.
    reference_key: SecretStr | None = None

    @field_validator("embedding_provider", mode="before")
    @classmethod
    def _default_embedding_provider(cls, value: object, info: ValidationInfo) -> object:
        if value in (None, ""):
            fixture = info.data.get("mode") is RuntimeMode.FIXTURE
            return "hashing" if fixture else "gemini"
        return value

    @field_validator("model_price_overrides", mode="before")
    @classmethod
    def _parse_price_overrides(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("must be a JSON object") from None
        return value

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
                raise ValueError(backend_env_name(name) + " must be at least 32 bytes")
        return self

    @model_validator(mode="after")
    def _check_budget_order(self) -> Self:
        if self.query_max_bytes > self.run_max_bytes:
            raise ValueError("QUERY_MAX_BYTES must not exceed RUN_MAX_BYTES")
        if self.model_first_token_seconds > self.model_request_max_seconds:
            raise ValueError(
                "MODEL_FIRST_TOKEN_SECONDS must not exceed MODEL_REQUEST_MAX_SECONDS"
            )
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("RETRY_BASE_SECONDS must not exceed RETRY_MAX_SECONDS")
        return self

    @model_validator(mode="after")
    def _require_live_settings(self) -> Self:
        if self.mode is RuntimeMode.LIVE:
            missing = [
                backend_env_name(name)
                for name in LIVE_REQUIRED_SETTINGS
                if getattr(self, name) is None
            ]
            if (
                self.execution_backend is ExecutionBackend.TEMPORAL
                and self.temporal_address is None
            ):
                missing.append(backend_env_name("temporal_address"))
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
            summary[backend_env_name(name)] = shown
        return summary


# OpenAI is the optional fallback provider, so it is not required in live mode.
# The signing key is required: every API route authenticates a bearer token.
# The Temporal address is required only with the Temporal execution backend.
LIVE_REQUIRED_SETTINGS: tuple[str, ...] = (
    "database_url",
    "bigquery_project",
    "gemini_api_key",
    "auth_signing_key",
)


def api_required_settings(settings: BackendSettings) -> tuple[str, ...]:
    """Settings the API needs with the selected execution backend."""
    names: tuple[str, ...] = ("database_url", "auth_signing_key")
    if settings.execution_backend is ExecutionBackend.TEMPORAL:
        names += ("temporal_address",)
    return names


def missing_api_settings(settings: BackendSettings) -> list[str]:
    """Variable names of unset settings the API needs (never values)."""
    return [
        backend_env_name(name)
        for name in api_required_settings(settings)
        if getattr(settings, name) is None
    ]


class CliSettings(BaseModel):
    """Settings for the ``analytics`` client. It holds no backend credentials."""

    model_config = _STRICT_MODEL

    api_url: str = "http://127.0.0.1:8080"
    timeout_seconds: float = Field(default=10.0, gt=0)
    # The bearer token (never printed) or a file holding it; the file wins when
    # both are set so a rotated token is picked up without editing the shell.
    token: SecretStr | None = None
    token_file: Path | None = None


# Field name -> environment variable, per settings model.
BACKEND_ENV_NAMES: dict[str, str] = {
    name: backend_env_name(name) for name in BackendSettings.model_fields
}
CLI_ENV_NAMES: dict[str, str] = {
    name: cli_env_name(name) for name in CliSettings.model_fields
}
# Every variable the application reads; the dotenv file may also hold COMPOSE_*.
KNOWN_ENV_NAMES: frozenset[str] = frozenset(
    {*BACKEND_ENV_NAMES.values(), *CLI_ENV_NAMES.values(), ENV_FILE_VARIABLE}
)
# The bare forms of the APP_ names (``DATABASE_URL``, ``MODE``...): never read,
# dropped from child environments, and pointed to their APP_ name when found in
# the env file.
BARE_GENERIC_NAMES: frozenset[str] = frozenset(
    {*(field.upper() for field in _BACKEND_NAME_OVERRIDES), "ENV_FILE"}
)
_LEGACY_SPECIAL: dict[str, str] = {
    LEGACY_BACKEND_PREFIX + "ENV_FILE": ENV_FILE_VARIABLE
}


def is_legacy_name(key: str) -> bool:
    return key.startswith(LEGACY_PREFIXES)


def legacy_replacement(key: str) -> str | None:
    """The current name of an older prefixed variable, else ``None``.

    Known settings map to their own names; an unknown legacy key just loses its
    prefix (``ANALYTICS_CLI_X`` becomes ``CLI_X``), so the loader then reports
    it as an unknown key instead of silently dropping it.
    """
    if key in _LEGACY_SPECIAL:
        return _LEGACY_SPECIAL[key]
    if key.startswith(LEGACY_BACKEND_PREFIX):
        field = key.removeprefix(LEGACY_BACKEND_PREFIX).lower()
        return BACKEND_ENV_NAMES.get(field, field.upper())
    if key.startswith(LEGACY_CLI_PREFIX):
        return cli_env_name(key.removeprefix(LEGACY_CLI_PREFIX).lower())
    return None


def load_backend_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> BackendSettings:
    return _validate(BackendSettings, BACKEND_ENV_NAMES, environ, env_file)


def load_cli_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> CliSettings:
    return _validate(CliSettings, CLI_ENV_NAMES, environ, env_file)


def _validate[M: BaseModel](
    model: type[M],
    names: Mapping[str, str],
    environ: Mapping[str, str] | None,
    env_file: Path | None,
) -> M:
    raw = _collect(names, environ, env_file)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_describe(exc, names)) from None


def _collect(
    names: Mapping[str, str],
    environ: Mapping[str, str] | None,
    env_file: Path | None,
) -> dict[str, str]:
    process = os.environ if environ is None else environ
    pointed = process.get(ENV_FILE_VARIABLE, "")
    custom_file = pointed != "" and env_file == DEFAULT_ENV_FILE
    if custom_file:
        env_file = Path(pointed)
        if not env_file.is_file():
            raise ConfigError([f"{ENV_FILE_VARIABLE}: file does not exist"])
    from_file: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        from_file = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    _check_names(from_file, process, env_file, explicit_file=custom_file)
    wanted = {env: field for field, env in names.items()}
    merged: dict[str, str] = {}
    for source in (from_file, process):
        for env, field in wanted.items():
            value = source.get(env)
            if value is not None:
                merged[field] = value
    return {field: value for field, value in merged.items() if value != ""}


def _check_names(
    from_file: Mapping[str, str],
    process: Mapping[str, str],
    env_file: Path | None,
    *,
    explicit_file: bool,
) -> None:
    """Refuse older prefixed names and unknown env-file keys (names, never values)."""
    problems: list[str] = []
    legacy_in_file = sorted(k for k in from_file if is_legacy_name(k))
    if legacy_in_file:
        command = MIGRATE_COMMAND
        if explicit_file or env_file != DEFAULT_ENV_FILE:
            command += f" --env-file {env_file}"
        problems.append(
            f"{env_file} uses names from before the rename: "
            + _renames(legacy_in_file)
            + f". Run `{command}` to rename them in place (values are kept); "
            "see README, 'Environment variables'"
        )
    legacy_in_process = sorted(k for k in process if is_legacy_name(k))
    if legacy_in_process:
        problems.append(
            "the process environment sets names from before the rename: "
            + _renames(legacy_in_process)
            + ". Unset them (or export the new names) in your shell; "
            f"`{MIGRATE_COMMAND}` migrates the environment file"
        )
    for key in sorted(from_file):
        if (
            key in KNOWN_ENV_NAMES
            or key.startswith(COMPOSE_ENV_PREFIX)
            or is_legacy_name(key)
        ):
            continue
        # A bare name given an APP_ prefix (``MODE`` for ``APP_MODE``) is the
        # likely hand rename; otherwise suggest the closest known name.
        expected = _BACKEND_NAME_OVERRIDES.get(key.lower())
        if key == "ENV_FILE":
            expected = ENV_FILE_VARIABLE
        close = (
            [expected]
            if expected
            else difflib.get_close_matches(key, sorted(KNOWN_ENV_NAMES), n=1)
        )
        hint = f" (did you mean {close[0]}?)" if close else ""
        problems.append(
            f"{env_file}: unknown variable {key}{hint}. Fix or remove it; "
            "the known names are in .env.example"
        )
    if problems:
        raise ConfigError(problems)


def _renames(keys: list[str]) -> str:
    return ", ".join(f"{key} -> {legacy_replacement(key)}" for key in keys)


def _describe(exc: ValidationError, names: Mapping[str, str]) -> list[str]:
    problems = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        name = names.get(location, location.upper()) if location else "configuration"
        problems.append(f"{name}: {error['msg']}")
    return problems
