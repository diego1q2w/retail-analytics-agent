"""Editor commands: ``retail-analytics-persona``. No web UI needed.

Runs with the backend's own settings against its database, acting as the
executive named by ``--as``. Authority is never taken from the command line:
the application checks that executive's current server-side roles
(``persona:edit``) on every command, exactly as the HTTP routes do.

    retail-analytics-persona show --as <executive-id>
    retail-analytics-persona history --as <executive-id>
    retail-analytics-persona check --file persona.txt
    retail-analytics-persona draft --as <id> --key <key> --file persona.txt
    retail-analytics-persona edit <draft-id> --as <id> --revision N --file p.txt
    retail-analytics-persona preview <draft-id> --as <id>
    retail-analytics-persona publish <draft-id> --as <id> --expected-current <id>
    retail-analytics-persona rollback <version-id> --as <id> --expected-current <id>
    retail-analytics-persona discard <draft-id> --as <id>
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import click

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.persona import PersonaService
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.bootstrap.persona import build_persona
from retail_analytics.domain.access import Permission
from retail_analytics.domain.persona import (
    PersonaError,
    PersonaFinding,
    PersonaVersion,
    screen_persona,
)

_ALL_SCOPES = frozenset(p.value for p in Permission)
_NONE = "none"


def _open() -> tuple[Persistence, PersonaService]:
    try:
        persistence = persistence_from_settings(load_backend_settings())
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    access = build_access(persistence, verifier=None)  # type: ignore[arg-type]
    return persistence, build_persona(persistence, access.resolver).service


def _run[T](
    actor: str, work: Callable[[PersonaService, Principal], Coroutine[Any, Any, T]]
) -> T:
    persistence, service = _open()
    try:
        return asyncio.run(work(service, Principal(actor, _ALL_SCOPES)))
    except AccessDenied:
        raise click.ClickException("not permitted") from None
    except PersonaError as error:
        raise click.ClickException(_describe(error)) from None
    finally:
        persistence.close()


def _describe(error: PersonaError) -> str:
    kinds = ", ".join(f"{f.kind.value}" for f in error.findings)
    suffix = f" [{kinds}]" if kinds else ""
    return f"{error.code.value}: {error.message}{suffix}"


def _text(file: Path | None, text: str | None) -> str:
    if (file is None) == (text is None):
        raise click.UsageError("give exactly one of --file or --text")
    return file.read_text(encoding="utf-8") if file is not None else text or ""


def _expected(value: str) -> str | None:
    return None if value == _NONE else value


def _findings(findings: tuple[PersonaFinding, ...]) -> str:
    return ", ".join(f"{f.kind.value}({f.severity.value})" for f in findings) or "none"


def _print_version(v: PersonaVersion) -> None:
    click.echo(
        f"version_id={v.version_id} number={v.number} state={v.state.value} "
        f"revision={v.revision} previewed={str(v.previewed).lower()} "
        f"findings={_findings(v.findings)}"
    )


@click.group()
def main() -> None:
    """Draft, preview, publish and roll back the company persona (editors)."""


actor_option = click.option(
    "--as", "actor", required=True, help="Executive ID acting (needs persona:edit)."
)
text_options = (
    click.option("--file", "file", type=click.Path(exists=True, path_type=Path)),
    click.option("--text", "text", type=str, default=None),
)


@main.command()
@actor_option
def show(actor: str) -> None:
    """Show the active persona."""
    current = _run(actor, lambda s, p: s.current(p))
    if current is None:
        click.echo("no persona is published")
        return
    _print_version(current)
    click.echo(current.content)


@main.command()
@actor_option
@click.option("--limit", type=click.IntRange(1, 100), default=20)
def history(actor: str, limit: int) -> None:
    """List versions and every publish/rollback, newest first."""
    result = _run(actor, lambda s, p: s.history(p, limit=limit))
    click.echo(f"active={result.current_version_id or _NONE}")
    for v in result.versions:
        _print_version(v)
    for item in result.publications:
        click.echo(
            f"publication={item.sequence} action={item.action.value} "
            f"version={item.version_id} number={item.version_number} "
            f"previous={item.previous_version_id or _NONE} by={item.actor_id} "
            f"at={item.at.isoformat()}"
        )


@main.command()
@text_options[0]
@text_options[1]
def check(file: Path | None, text: str | None) -> None:
    """Flag text that conflicts with fixed policy (stores nothing)."""
    from retail_analytics.domain.persona import clean_content

    try:
        findings = screen_persona(clean_content(_text(file, text)))
    except PersonaError as error:
        raise click.ClickException(_describe(error)) from None
    click.echo(f"findings={_findings(findings)}")
    if any(f.blocking for f in findings):
        raise SystemExit(1)


@main.command()
@actor_option
@click.option(
    "--key", required=True, help="Idempotency key: a retry returns the draft."
)
@text_options[0]
@text_options[1]
def draft(actor: str, key: str, file: Path | None, text: str | None) -> None:
    """Create a draft from free text."""
    content = _text(file, text)
    created = _run(actor, lambda s, p: s.create_draft(p, content, idempotency_key=key))
    _print_version(created)


@main.command()
@click.argument("draft_id")
@actor_option
@click.option("--revision", type=click.IntRange(min=1), required=True)
@text_options[0]
@text_options[1]
def edit(
    draft_id: str, actor: str, revision: int, file: Path | None, text: str | None
) -> None:
    """Replace your draft's text (clears its preview)."""
    content = _text(file, text)
    updated = _run(
        actor,
        lambda s, p: s.update_draft(p, draft_id, content, expected_revision=revision),
    )
    _print_version(updated)


@main.command()
@click.argument("draft_id")
@actor_option
def discard(draft_id: str, actor: str) -> None:
    """Discard your draft."""
    _print_version(_run(actor, lambda s, p: s.discard_draft(p, draft_id)))


@main.command()
@click.argument("draft_id")
@actor_option
def preview(draft_id: str, actor: str) -> None:
    """Show the current and proposed persona over the same sample findings."""
    result = _run(actor, lambda s, p: s.preview(p, draft_id))
    click.echo(f"draft={result.draft_id} number={result.draft_number}")
    click.echo(f"active_version_id={result.current_version_id or _NONE}")
    click.echo(f"findings={_findings(result.findings)}")
    click.echo(f"persona_applied={str(result.persona_applied).lower()}")
    click.echo(f"preserved={str(result.preserved).lower()}")
    if result.missing_proposed:
        click.echo("missing_from_proposed=" + "; ".join(result.missing_proposed))
    click.echo("--- current sample ---")
    click.echo(result.sample_current)
    click.echo("--- proposed sample ---")
    click.echo(result.sample_proposed)
    click.echo("--- proposed instructions ---")
    click.echo(result.proposed_instructions)


@main.command()
@click.argument("draft_id")
@actor_option
@click.option(
    "--expected-current",
    required=True,
    help="The active version id you previewed against, or 'none'.",
)
def publish(draft_id: str, actor: str, expected_current: str) -> None:
    """Make the previewed draft the active persona (new runs only)."""
    result = _run(
        actor,
        lambda s, p: s.publish(
            p, draft_id, expected_current=_expected(expected_current)
        ),
    )
    click.echo(
        f"published number={result.version_number} version={result.version_id} "
        f"sequence={result.sequence}"
    )


@main.command()
@click.argument("version_id")
@actor_option
@click.option("--expected-current", required=True, help="Active version id, or 'none'.")
def rollback(version_id: str, actor: str, expected_current: str) -> None:
    """Make an earlier published version active again."""
    result = _run(
        actor,
        lambda s, p: s.rollback(
            p, version_id, expected_current=_expected(expected_current)
        ),
    )
    click.echo(
        f"rolled back to number={result.version_number} version={result.version_id} "
        f"sequence={result.sequence}"
    )


if __name__ == "__main__":
    main()
