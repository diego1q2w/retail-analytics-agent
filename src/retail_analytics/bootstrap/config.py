"""Typed configuration loaded once at the composition roots.

Backend settings use the ``RETAIL_ANALYTICS_`` prefix; CLI client settings use
``ANALYTICS_CLI_``. Values come from an optional dotenv file overlaid by the
process environment. Empty values count as unset.

Validation errors name the offending variable and the problem, never the value,
so secrets cannot leak through error output. Inner layers never read the
environment: bootstrap passes the typed values they need.
"""

from __future__ import annotations

import os
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
    model_validator,
)

BACKEND_ENV_PREFIX = "RETAIL_ANALYTICS_"
CLI_ENV_PREFIX = "ANALYTICS_CLI_"
DEFAULT_ENV_FILE = Path(".env")


class ConfigError(Exception):
    """Invalid or incomplete configuration. The message never contains values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n  " + "\n  ".join(problems))


class RuntimeMode(StrEnum):
    """``fixture``: offline deterministic fakes. ``live``: real services."""

    FIXTURE = "fixture"
    LIVE = "live"


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
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3-flash-preview"
    openai_api_key: SecretStr | None = None
    # Golden retrieval: "hashing" is the offline deterministic embedder.
    embedding_provider: Literal["hashing", "gemini"] = "hashing"
    embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = Field(default=768, ge=128, le=3072)
    retrieval_max_results: int = Field(default=3, ge=1, le=3)
    retrieval_channel_candidates: int = Field(default=10, ge=3, le=100)
    retrieval_min_similarity: float = Field(default=0.55, ge=-1.0, le=1.0)
    retrieval_min_lexical_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    # Local (simulated) token authentication; see README "Authentication".
    auth_issuer: str = Field(default="retail-analytics-local", min_length=1)
    auth_audience: str = Field(default="retail-analytics-api", min_length=1)
    auth_signing_key: SecretStr | None = None

    @model_validator(mode="after")
    def _check_signing_key(self) -> Self:
        key = self.auth_signing_key
        if key is not None and len(key.get_secret_value().encode()) < 32:
            raise ValueError(
                BACKEND_ENV_PREFIX + "AUTH_SIGNING_KEY must be at least 32 bytes"
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
LIVE_REQUIRED_SETTINGS: tuple[str, ...] = (
    "database_url",
    "temporal_address",
    "bigquery_project",
    "gemini_api_key",
)


class CliSettings(BaseModel):
    """Settings for the ``analytics`` client. It holds no backend credentials."""

    model_config = _STRICT_MODEL

    api_url: str = "http://127.0.0.1:8080"
    timeout_seconds: float = Field(default=10.0, gt=0)


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
    if env_file is not None and env_file.is_file():
        merged.update(
            {k: v for k, v in dotenv_values(env_file).items() if v is not None}
        )
    merged.update(os.environ if environ is None else environ)
    return {
        key.removeprefix(prefix).lower(): value
        for key, value in merged.items()
        if key.startswith(prefix) and value != ""
    }


def _describe(exc: ValidationError, prefix: str) -> list[str]:
    problems = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        name = prefix + location.upper() if location else "configuration"
        problems.append(f"{name}: {error['msg']}")
    return problems
