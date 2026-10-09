"""Reconcile a local ``.env`` with ``.env.example`` for the one-command bootstrap.

Pure text logic, no I/O except through the small ``write_env_file`` helper. The
key list is read from the template, never hardcoded: a key added to
``.env.example`` by a later task shows up in every environment file on the next
run. Only keys that need special treatment appear in the rule tables below.

Guarantees:

- An existing file is never rewritten when nothing needs filling (byte-for-byte
  identical), existing non-empty values are never changed, and lines are never
  reordered. Missing keys are appended; empty values are filled in place.
- Reports and messages carry key names and statuses only, never values.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

PREFIX = "RETAIL_ANALYTICS_"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

# Local-only secrets that are safe to generate (the keys must be >= 32 bytes).
GENERATED_SECRETS: frozenset[str] = frozenset(
    {PREFIX + "AUTH_SIGNING_KEY", PREFIX + "REFERENCE_KEY"}
)
REFERENCE_KEY = PREFIX + "REFERENCE_KEY"
REFERENCE_KEY_NOTE = "never regenerate: rotating it invalidates existing references"
# Passwords of the Compose PostgreSQL volume. Generated only for a new volume:
# the server applies them once, when it first initialises an empty volume.
COMPOSE_VOLUME_PASSWORDS: dict[str, str] = {
    "COMPOSE_APP_DB_PASSWORD": "local-only-app",
    "COMPOSE_TEMPORAL_DB_PASSWORD": "local-only-temporal",
    "COMPOSE_PG_ADMIN_PASSWORD": "local-only-admin",
}
POSTGRES_PORT_KEY = "COMPOSE_POSTGRES_PORT"
TEMPORAL_PORT_KEY = "COMPOSE_TEMPORAL_PORT"
DEFAULT_POSTGRES_PORT = "55442"
DEFAULT_TEMPORAL_PORT = "57233"
DATABASE_URL_KEY = PREFIX + "DATABASE_URL"
TEMPORAL_ADDRESS_KEY = PREFIX + "TEMPORAL_ADDRESS"
EXECUTION_BACKEND_KEY = PREFIX + "EXECUTION_BACKEND"
EXECUTION_BACKENDS = ("local", "temporal")
DEFAULT_EXECUTION_BACKEND = "local"
# Shown once, when an existing environment file (from before the selector
# existed) gets the key: it adopts the new local default.
LOCAL_ADOPTED_NOTE = (
    f"{EXECUTION_BACKEND_KEY} was added as local, the new default: "
    "investigations now run inside the API process with PostgreSQL only, and "
    "Temporal and the worker are no longer started. Existing Temporal settings, "
    "containers and data are kept, unused. To keep Temporal execution, set it "
    "to temporal (finish or cancel active investigations before switching; "
    "see README, 'Temporal execution')."
)
# External credentials: never generated; the action says what the user does.
EXTERNAL_CREDENTIALS: dict[str, tuple[str, bool]] = {
    # key -> (action, secret input)
    PREFIX + "BIGQUERY_PROJECT": (
        "set your Google Cloud project, see docs/google-access.md",
        False,
    ),
    PREFIX + "GEMINI_API_KEY": (
        "create a Gemini key in Google AI Studio, see docs/google-access.md",
        True,
    ),
    PREFIX + "OPENAI_API_KEY": (
        "optional fallback provider key, see docs/google-access.md",
        True,
    ),
}
OPTIONAL_EXTERNAL: frozenset[str] = frozenset({PREFIX + "OPENAI_API_KEY"})
_SECRET_NAME = re.compile(r"(KEY|PASSWORD|SECRET|TOKEN|URL)$")


class Status(StrEnum):
    GENERATED = "generated"
    KEPT = "kept"
    DEFAULT = "default"
    MISSING = "missing"
    EMPTY = "empty"


@dataclass(frozen=True)
class KeyReport:
    key: str
    status: Status
    detail: str = ""

    def render(self) -> str:
        if self.status is Status.MISSING:
            return f"<missing: {self.detail}>"
        if self.detail:
            return f"<{self.status}: {self.detail}>"
        return f"<{self.status}>"


@dataclass(frozen=True)
class Reconciled:
    text: str
    changed: bool
    reports: tuple[KeyReport, ...]
    warnings: tuple[str, ...] = field(default=())


def parse_line(raw: str) -> tuple[str, str] | None:
    """Return ``(key, value)`` for an assignment line, else ``None``."""
    match = _LINE.match(raw)
    if match is None:
        return None
    return match.group(1), _unquote(match.group(2).strip())


def _unquote(value: str) -> str:
    if value[:1] in {'"', "'"}:
        end = value.find(value[0], 1)
        return value[1:end] if end != -1 else value[1:]
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_values(text: str) -> dict[str, str]:
    """Assignments in file order; the last occurrence of a key wins."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        parsed = parse_line(raw)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def is_secret_name(key: str) -> bool:
    return _SECRET_NAME.search(key) is not None


def secret_values(values: Mapping[str, str]) -> list[str]:
    """Values to scrub from any output that might echo them."""
    return [v for k, v in values.items() if is_secret_name(k) and len(v) >= 8]


def redact(text: str, secrets_to_hide: list[str]) -> str:
    for value in sorted(secrets_to_hide, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return text


def database_url(values: Mapping[str, str]) -> str:
    password = values.get("COMPOSE_APP_DB_PASSWORD") or "local-only-app"
    port = values.get(POSTGRES_PORT_KEY) or DEFAULT_POSTGRES_PORT
    return (
        f"postgresql+psycopg://retail_app:{quote(password, safe='')}"
        f"@127.0.0.1:{port}/retail_app"
    )


def temporal_address(values: Mapping[str, str]) -> str:
    return "127.0.0.1:" + (values.get(TEMPORAL_PORT_KEY) or DEFAULT_TEMPORAL_PORT)


def reconcile(
    template: str,
    existing: str | None,
    *,
    new_postgres_volume: bool = True,
    overrides: Mapping[str, str] | None = None,
    prompt: Callable[[str, bool], str] | None = None,
) -> Reconciled:
    """Return the environment file text that bootstrap wants.

    ``existing`` is ``None`` when there is no file yet. ``overrides`` fill empty
    keys (for example chosen ports). ``prompt(key, secret)`` is asked for empty
    external credentials when interactive; it returns ``""`` to skip.
    """
    template_values = parse_values(template)
    base = template if existing is None else existing
    lines = base.splitlines()
    present = parse_values(base)
    appended = [
        f"{key}={value}" for key, value in template_values.items() if key not in present
    ]
    if existing is not None and appended:
        lines.extend(
            ["", "# Added by scripts/bootstrap.sh from .env.example", *appended]
        )
    new_keys = (
        set(template_values)
        if existing is None
        else {key for key in template_values if key not in present}
    )
    for key, value in (overrides or {}).items():
        if key in new_keys and value:
            index = max(i for i, raw in enumerate(lines) if _key_of(raw) == key)
            lines[index] = _assignment(lines[index], key, value)
    values = parse_values("\n".join(lines))
    warnings: list[str] = []
    reports: dict[str, KeyReport] = {}
    filled: set[str] = set()

    def fill(key: str, value: str) -> None:
        index = max(i for i, raw in enumerate(lines) if _key_of(raw) == key)
        lines[index] = _assignment(lines[index], key, value)
        values[key] = value
        filled.add(key)

    def settle(key: str) -> None:
        if values.get(key, "") != "":
            note = REFERENCE_KEY_NOTE if key == REFERENCE_KEY else ""
            reports[key] = KeyReport(key, Status.KEPT, note)
            return
        if overrides and overrides.get(key):
            fill(key, overrides[key])
            reports[key] = KeyReport(key, Status.DEFAULT, "from option")
        elif key in GENERATED_SECRETS:
            fill(key, secrets.token_urlsafe(48))
            note = REFERENCE_KEY_NOTE if key == REFERENCE_KEY else ""
            reports[key] = KeyReport(key, Status.GENERATED, note)
        elif key in COMPOSE_VOLUME_PASSWORDS:
            if new_postgres_volume:
                fill(key, secrets.token_urlsafe(24))
                reports[key] = KeyReport(key, Status.GENERATED)
            else:
                reports[key] = KeyReport(
                    key, Status.KEPT, "existing volume keeps its current default"
                )
        elif key == DATABASE_URL_KEY:
            fill(key, database_url(values))
            reports[key] = KeyReport(key, Status.DEFAULT, "local compose stack")
        elif key == TEMPORAL_ADDRESS_KEY:
            fill(key, temporal_address(values))
            reports[key] = KeyReport(key, Status.DEFAULT, "local compose stack")
        elif key in EXTERNAL_CREDENTIALS:
            action, secret_input = EXTERNAL_CREDENTIALS[key]
            answer = prompt(key, secret_input) if prompt else ""
            if answer:
                fill(key, answer)
                reports[key] = KeyReport(key, Status.KEPT, "entered")
            elif key in OPTIONAL_EXTERNAL:
                reports[key] = KeyReport(key, Status.EMPTY, "optional")
            else:
                reports[key] = KeyReport(key, Status.MISSING, action)
        elif template_values.get(key, ""):
            fill(key, template_values[key])
            reports[key] = KeyReport(key, Status.DEFAULT)
        else:
            reports[key] = KeyReport(key, Status.EMPTY)

    # Ports and volume passwords first: the connection defaults are built from them.
    ordered = sorted(
        template_values,
        key=lambda k: 0 if k.startswith("COMPOSE_") else 1,
    )
    for key in ordered:
        settle(key)
    if (
        existing is not None
        and EXECUTION_BACKEND_KEY in new_keys
        and values.get(EXECUTION_BACKEND_KEY) == DEFAULT_EXECUTION_BACKEND
    ):
        warnings.append(LOCAL_ADOPTED_NOTE)
    if not new_postgres_volume and not any(
        values.get(k) for k in COMPOSE_VOLUME_PASSWORDS
    ):
        warnings.append(
            "an existing postgres volume was found: the local default database "
            "passwords stay in use (remove the volume to get generated ones)"
        )
    changed = existing is None or bool(filled) or bool(appended)
    text = "\n".join(lines) + "\n" if changed or existing is None else existing
    ordered_reports = tuple(reports[key] for key in template_values)
    return Reconciled(text, changed, ordered_reports, tuple(warnings))


def _key_of(raw: str) -> str | None:
    parsed = _LINE.match(raw)
    return parsed.group(1) if parsed else None


def _assignment(raw: str, key: str, value: str) -> str:
    """Rewrite ``raw`` keeping its ``export`` prefix and indentation."""
    prefix = re.match(r"^(\s*(?:export\s+)?)", raw)
    lead = prefix.group(1) if prefix else ""
    return f"{lead}{key}={_format(value)}"


def _format(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.:/@+%=,-]*", value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def write_env_file(path: Path, text: str) -> None:
    """Atomically write ``text``; new files are private (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
