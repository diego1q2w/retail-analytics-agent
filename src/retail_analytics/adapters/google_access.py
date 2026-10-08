"""Google SDK adapters for the access-check ports.

Every SDK failure becomes an ``AccessError`` built from fixed text, the HTTP
status or exception class, so no token, key or raw SDK message is ever echoed.
"""

from __future__ import annotations

from google import genai
from google.api_core import exceptions as api_exceptions
from google.auth import default as default_credentials
from google.auth import exceptions as auth_exceptions
from google.cloud import bigquery
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from retail_analytics.application.access_check import (
    DRY_RUN_BYTES_LIMIT,
    AccessError,
    TableMetadata,
)

LOGIN_REMEDY = (
    "run `gcloud auth application-default login` and set the quota project with "
    "`gcloud auth application-default set-quota-project <project>`"
)
BIGQUERY_SCOPES = ("https://www.googleapis.com/auth/bigquery",)


def _bigquery_error(exc: Exception, project: str) -> AccessError:
    if isinstance(exc, auth_exceptions.RefreshError):
        return AccessError(
            "credentials are expired or revoked", f"re-authenticate: {LOGIN_REMEDY}"
        )
    if isinstance(exc, auth_exceptions.DefaultCredentialsError):
        return AccessError("no application default credentials", LOGIN_REMEDY)
    if isinstance(exc, api_exceptions.NotFound):
        return AccessError(
            "table or project not found",
            f"check that project '{project}' exists and the dataset name is right",
        )
    if isinstance(exc, api_exceptions.Forbidden):
        return AccessError(
            "permission denied (HTTP 403)",
            f"enable the BigQuery API on '{project}' and make sure your account may "
            "create query jobs there (billing is not required in the sandbox)",
        )
    if isinstance(exc, api_exceptions.Unauthorized):
        return AccessError("credentials rejected (HTTP 401)", LOGIN_REMEDY)
    return AccessError(
        f"unexpected BigQuery failure ({type(exc).__name__})",
        "rerun; if it persists check network access and the project setting",
    )


class BigQueryWarehouseAccess:
    """Implements ``WarehouseAccess`` with a lazily created BigQuery client."""

    def __init__(
        self, project: str, location: str, client: bigquery.Client | None = None
    ) -> None:
        self._project = project
        self._location = location
        self._client = client

    def _get_client(self) -> bigquery.Client:
        if self._client is None:
            credentials, _ = default_credentials(scopes=list(BIGQUERY_SCOPES))
            self._client = bigquery.Client(
                project=self._project,
                credentials=credentials,
                location=self._location,
            )
        return self._client

    def credentials_ready(self) -> None:
        try:
            self._get_client()
        except Exception as exc:
            raise _bigquery_error(exc, self._project) from None

    def table_metadata(self, table: str) -> TableMetadata:
        try:
            info = self._get_client().get_table(table)
        except Exception as exc:
            raise _bigquery_error(exc, self._project) from None
        return TableMetadata(table, info.num_rows or 0, len(info.schema))

    def dry_run_bytes(self, sql: str) -> int:
        config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            maximum_bytes_billed=DRY_RUN_BYTES_LIMIT,
        )
        try:
            job = self._get_client().query(sql, job_config=config)
        except Exception as exc:
            raise _bigquery_error(exc, self._project) from None
        return int(job.total_bytes_processed or 0)


def _gemini_error(exc: Exception, model: str) -> AccessError:
    if isinstance(exc, genai_errors.APIError):
        code = exc.code
        if code in (400, 401, 403):
            return AccessError(
                f"API key rejected or not permitted (HTTP {code})",
                "create a key in Google AI Studio and set "
                "RETAIL_ANALYTICS_GEMINI_API_KEY in your ignored .env",
            )
        if code == 404:
            return AccessError(
                f"model '{model}' not available to this key (HTTP 404)",
                "set RETAIL_ANALYTICS_GEMINI_MODEL to a model that supports "
                "generateContent for your key",
            )
        if code == 429:
            return AccessError(
                "rate limit or quota exceeded (HTTP 429)",
                "wait and retry, or check the quota for your key in AI Studio",
            )
        return AccessError(
            f"Gemini API error (HTTP {code})", "rerun; check Google AI status"
        )
    return AccessError(
        f"unexpected Gemini failure ({type(exc).__name__})",
        "rerun; if it persists check network access",
    )


class GeminiModelAccess:
    """Implements ``ModelAccess`` against the Gemini Developer API."""

    def __init__(
        self, api_key: str, model: str, client: genai.Client | None = None
    ) -> None:
        self._model = model
        self._client = client or genai.Client(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self._model

    def ping(self) -> None:
        try:
            self._client.models.generate_content(
                model=self._model,
                contents="Reply with the single word: ok",
                config=genai_types.GenerateContentConfig(max_output_tokens=256),
            )
        except Exception as exc:
            raise _gemini_error(exc, self._model) from None
