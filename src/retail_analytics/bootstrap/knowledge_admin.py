"""Local Golden example commands: ``retail-analytics-knowledge``. Development only.

Runs with the backend's own settings against its database, acting as the
executive named by ``--as``. Authority is never taken from the command line:
the application checks that executive's current server-side roles and
products on every command (``analysis:read`` to submit, ``knowledge:review``
for the rest), exactly as for any other caller.

Local self-publication policy: this command, and nothing else, lets the local
demo administrator (``exec-local-admin``, provisioned by
``retail-analytics-dev-access provision``) approve their own example, so the
person evaluating the demo needs no second identity. Every such review is
recorded with ``self_published=true`` and is never independent review. Every
other executive keeps the independent-reviewer rule here too, and the API,
the seed command and the agent never use this policy.

    retail-analytics-knowledge submit --as exec-local-admin --key k1 --file ex.json
    retail-analytics-knowledge queue --as exec-local-admin
    retail-analytics-knowledge show <example-id> <version> --as exec-local-admin
    retail-analytics-knowledge approve <example-id> <version> --as exec-local-admin \\
        --rationale "..." --correct --sanitized --applicable
    retail-analytics-knowledge history <example-id> <version> --as exec-local-admin

The example file is JSON: ``question``, ``sql``, ``method_summary``,
``report_markdown``, ``metrics`` (list of ``{"metric_id", "version"}``),
``sanitization_attested`` (true for shared examples), and optionally
``schema_version`` (default: the current logical catalog),
``restricted_product_ids`` (default: shared) and ``origin``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import click

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.golden_seed_library import SEED_SCHEMA_VERSION
from retail_analytics.application.knowledge import (
    SELF_PUBLISHED_CHECK,
    ApprovalChecks,
    ExampleDraft,
    KnowledgeError,
    KnowledgeService,
)
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.dev_access import LOCAL_ADMIN
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.bootstrap.knowledge import build_knowledge
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.knowledge import (
    Applicability,
    ExampleRef,
    KnowledgeAccess,
    MetricRef,
    Origin,
    Provenance,
    SourceKind,
)

# The only identity the local self-publication policy names.
LOCAL_SELF_PUBLISHERS = frozenset({LOCAL_ADMIN.executive_id})

_ALL_SCOPES = frozenset(p.value for p in Permission)


def _open() -> tuple[Persistence, KnowledgeService]:
    try:
        settings = load_backend_settings()
        persistence = persistence_from_settings(settings)
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    access = build_access(persistence, verifier=None)  # type: ignore[arg-type]
    services = build_knowledge(
        persistence,
        build_artifacts(settings, persistence),
        access.resolver,
        self_publishers=LOCAL_SELF_PUBLISHERS,
    )
    return persistence, services.service


def _run[T](
    actor: str,
    work: Callable[[KnowledgeService, Principal], Coroutine[Any, Any, T]],
) -> T:
    persistence, service = _open()
    try:
        return asyncio.run(work(service, Principal(actor, _ALL_SCOPES)))
    except AccessDenied:
        raise click.ClickException("not permitted") from None
    except KnowledgeError as error:
        kinds = ", ".join(f"{field}:{kind}" for field, kind in error.findings)
        suffix = f" [{kinds}]" if kinds else ""
        raise click.ClickException(
            f"{error.code.value}: {error.message}{suffix}"
        ) from None
    finally:
        persistence.close()


def draft_from_json(text: str) -> ExampleDraft:
    """Parse an example file; raises ``click.UsageError`` naming the problem."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("the file must hold one JSON object")
        metrics = frozenset(
            MetricRef(str(m["metric_id"]), int(m["version"]))
            for m in data.get("metrics", [])
        )
        restricted = frozenset(str(p) for p in data.get("restricted_product_ids", []))
        return ExampleDraft(
            question=str(data["question"]),
            sql=str(data["sql"]),
            method_summary=str(data["method_summary"]),
            report_markdown=str(data["report_markdown"]),
            applicability=Applicability(
                str(data.get("schema_version", SEED_SCHEMA_VERSION)), metrics
            ),
            access=KnowledgeAccess(restricted),
            origin=Origin(data.get("origin", Origin.PROJECT_AUTHORED.value)),
            provenance=Provenance(SourceKind.AUTHORED),
            sanitization_attested=data.get("sanitization_attested") is True,
        )
    except KeyError as exc:
        raise click.UsageError(f"example file: missing field {exc}") from None
    except (TypeError, ValueError) as exc:
        raise click.UsageError(f"example file: {exc}") from None


actor_option = click.option(
    "--as", "actor", required=True, help="Executive ID acting (roles are checked)."
)


@click.group()
def main() -> None:
    """Submit, review and publish Golden examples (local development)."""


@main.command()
@actor_option
@click.option("--key", required=True, help="Idempotency key: a retry is a no-op.")
@click.option(
    "--file",
    "file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--example-id", default=None, help="Next version of this example.")
def submit(actor: str, key: str, file: Path, example_id: str | None) -> None:
    """Submit an example as a candidate (never delivered until approved)."""
    draft = draft_from_json(file.read_text(encoding="utf-8"))
    version = _run(
        actor,
        lambda s, p: s.submit_candidate(
            p, draft, idempotency_key=key, example_id=example_id
        ),
    )
    click.echo(
        f"example={version.example_id} version={version.version} "
        f"status={version.status.value}"
    )


@main.command()
@actor_option
def queue(actor: str) -> None:
    """List candidates waiting for review (metadata only)."""
    rows = _run(actor, lambda s, p: s.review_queue(p))
    if not rows:
        click.echo("no candidates")
    for row in rows:
        click.echo(
            f"example={row.ref.example_id} version={row.ref.version} "
            f"author={row.author_id} shared={str(row.shared).lower()} "
            f"origin={row.origin.value}"
        )


@main.command()
@click.argument("example_id")
@click.argument("version", type=click.IntRange(min=1))
@actor_option
def show(example_id: str, version: int, actor: str) -> None:
    """Show a version's content for review."""
    ref = ExampleRef(example_id, version)
    example = _run(actor, lambda s, p: s.read_for_review(p, ref))
    click.echo(f"question: {example.question}")
    click.echo(f"method: {example.method_summary}")
    click.echo("--- sql ---")
    click.echo(example.sql)
    click.echo("--- report ---")
    click.echo(example.report_markdown)


@main.command()
@click.argument("example_id")
@click.argument("version", type=click.IntRange(min=1))
@actor_option
@click.option("--rationale", required=True)
@click.option("--correct", is_flag=True, help="You checked the SQL and figures.")
@click.option("--sanitized", is_flag=True, help="You checked it has no personal data.")
@click.option("--applicable", is_flag=True, help="It fits the current definitions.")
def approve(
    example_id: str,
    version: int,
    actor: str,
    rationale: str,
    correct: bool,
    sanitized: bool,
    applicable: bool,
) -> None:
    """Publish a candidate (all three checks are required)."""
    ref = ExampleRef(example_id, version)
    checks = ApprovalChecks(correct=correct, sanitized=sanitized, applicable=applicable)
    result = _run(
        actor, lambda s, p: s.approve(p, ref, rationale=rationale, checks=checks)
    )
    review = (
        "self-published (local policy; not independent review)"
        if result.version.author_id == actor
        else "independent"
    )
    click.echo(
        f"example={result.version.example_id} version={result.version.version} "
        f"status={result.version.status.value} review={review}"
    )


@main.command()
@click.argument("example_id")
@click.argument("version", type=click.IntRange(min=1))
@actor_option
def history(example_id: str, version: int, actor: str) -> None:
    """Every review event of a version, oldest first."""
    ref = ExampleRef(example_id, version)
    events = _run(actor, lambda s, p: s.history(p, ref))
    for event in events:
        self_published = bool((event.checks or {}).get(SELF_PUBLISHED_CHECK))
        click.echo(
            f"{event.at.isoformat()} {event.action.value} by={event.actor_id} "
            f"to={event.to_status.value} "
            f"self_published={str(self_published).lower()}"
        )


if __name__ == "__main__":
    main()
