"""One-command local bootstrap: ``./scripts/bootstrap.sh``.

Runs the ordered, idempotent ``STEPS`` below: environment file, Docker check,
local services, migrations, demo executives, Golden seeds, verification. Safe to
rerun; a rerun changes nothing that is already in place.

The services follow the execution backend (``EXECUTION_BACKEND``
or ``--execution-backend``): PostgreSQL only with ``local`` (the default), plus
Temporal and its namespace with ``temporal``. Nothing is ever stopped or removed.

To make a later feature part of the bootstrap, append a ``BootstrapStep`` to
``STEPS`` (README, "How to add a bootstrap step"); do not add a separate setup
script. Steps shell out to the project's own commands (``python -m ...``,
``docker compose``) with the environment file's values, so a step behaves
exactly as the same command run by hand.

Output never contains secret values: only key names and statuses are printed,
and captured child output is scrubbed of every secret-looking value in the
environment file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import click

from retail_analytics.bootstrap import local_env
from retail_analytics.bootstrap.config import (
    BARE_GENERIC_NAMES,
    ENV_FILE_VARIABLE,
    KNOWN_ENV_NAMES,
    is_legacy_name,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECT = "retail-analytics-local"  # the `name:` in compose.yaml
SERVICES_TIMEOUT = 900
STEP_TIMEOUT = 300


def is_config_name(key: str) -> bool:
    """A variable the application reads, its bare generic form (for example
    ``DATABASE_URL`` for ``APP_DATABASE_URL``), or an older prefixed one."""
    return key in KNOWN_ENV_NAMES or key in BARE_GENERIC_NAMES or is_legacy_name(key)


Echo = Callable[[str], None]


class StepFailed(Exception):
    """A required step could not complete. The message must not contain secrets."""


@dataclass(frozen=True)
class StepResult:
    status: Literal["done", "skipped", "warning"] = "done"
    message: str = ""


@dataclass
class SetupContext:
    root: Path
    env_file: Path
    project: str
    telemetry: bool = True
    interactive: bool = False
    postgres_port: str | None = None
    temporal_port: str | None = None
    # ``--execution-backend``: this run's choice, over the environment file.
    execution_backend: str | None = None
    echo: Echo = click.echo
    values: dict[str, str] = field(default_factory=dict)

    def refresh_values(self) -> None:
        text = self.env_file.read_text(encoding="utf-8")
        self.values = local_env.parse_values(text)

    def child_env(self) -> dict[str, str]:
        """Environment for child commands, isolated from stray configuration.

        Parent-shell configuration (every setting name the application reads,
        the bare forms of the ``APP_`` names such as ``DATABASE_URL``, and the
        older ``RETAIL_ANALYTICS_*`` / ``ANALYTICS_CLI_*`` names) is
        dropped, the environment file's non-empty values are added (except
        older names, which the loader reports from the file itself), and
        ``APP_ENV_FILE`` points the typed loader at that file so the repository
        ``.env`` is never read. Other variables (PATH, DOCKER_*, COMPOSE_*)
        pass through.
        """
        env = {k: v for k, v in os.environ.items() if not is_config_name(k)}
        env.update(
            {k: v for k, v in self.values.items() if v != "" and not is_legacy_name(k)}
        )
        if self.execution_backend:
            env[local_env.EXECUTION_BACKEND_KEY] = self.execution_backend
        env[ENV_FILE_VARIABLE] = str(self.env_file)
        return env

    def run(
        self, argv: Sequence[str], *, timeout: int = STEP_TIMEOUT, show: bool = False
    ) -> str:
        """Run a command; return scrubbed output. Raise ``StepFailed`` on failure."""
        try:
            done = subprocess.run(  # noqa: S603
                list(argv),
                cwd=self.root,
                env=self.child_env(),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise StepFailed(f"`{_label(argv)}` timed out after {timeout}s") from None
        hidden = local_env.secret_values(self.values)
        output = local_env.redact((done.stdout + done.stderr).strip(), hidden)
        if done.returncode != 0:
            label = local_env.redact(_label(argv), hidden)
            raise StepFailed(
                f"`{label}` exited with status {done.returncode}\n"
                + _indent(output[-2000:])
            )
        if show and output:
            self.echo(_indent(output))
        return output

    def python(self, *args: str, show: bool = False) -> str:
        return self.run([sys.executable, *args], show=show)

    def compose(self, *args: str, timeout: int = SERVICES_TIMEOUT) -> str:
        return self.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(self.env_file),
                "-f",
                str(self.root / "compose.yaml"),
                "-p",
                self.project,
                *args,
            ],
            timeout=timeout,
        )


@dataclass(frozen=True)
class BootstrapStep:
    """One idempotent unit of local setup.

    ``run`` returns a ``StepResult`` or raises ``StepFailed``. ``enabled`` lets a
    step opt out (for example telemetry, which is a flag). ``required`` steps stop
    the bootstrap on failure; a failure of an optional step is only a warning.
    """

    name: str
    summary: str
    run: Callable[[SetupContext], StepResult]
    enabled: Callable[[SetupContext], bool] = lambda ctx: True
    required: bool = True


def _label(argv: Sequence[str]) -> str:
    return " ".join(Path(argv[0]).name if i == 0 else a for i, a in enumerate(argv))[
        :200
    ]


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


# --- steps ----------------------------------------------------------------


def _postgres_volume_exists(ctx: SetupContext) -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(  # noqa: S603
        ["docker", "volume", "inspect", f"{ctx.project}_postgres-data"],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=60,
    )
    return probe.returncode == 0


def step_environment(ctx: SetupContext) -> StepResult:
    template_path = ctx.root / ".env.example"
    if not template_path.is_file():
        raise StepFailed(".env.example is missing from the repository root")
    existing = (
        ctx.env_file.read_text(encoding="utf-8") if ctx.env_file.is_file() else None
    )
    migrated = local_env.migrate_legacy(existing) if existing is not None else None
    if migrated is not None and migrated.changed:
        existing = migrated.text
    overrides = {
        k: v
        for k, v in (
            (local_env.POSTGRES_PORT_KEY, ctx.postgres_port),
            (local_env.TEMPORAL_PORT_KEY, ctx.temporal_port),
            # Telemetry is on by default (.env.example says true). Opting out
            # on a new or completed environment file writes false; an explicit
            # value already in the file is never overwritten.
            (local_env.TELEMETRY_ENABLED_KEY, "" if ctx.telemetry else "false"),
            # Written only to a new key; an explicit choice is never changed.
            (local_env.EXECUTION_BACKEND_KEY, ctx.execution_backend),
        )
        if v
    }

    def ask(key: str, secret: bool) -> str:
        return str(
            click.prompt(
                f"{key} (Enter to skip)",
                default="",
                show_default=False,
                hide_input=secret,
            )
        ).strip()

    result = local_env.reconcile(
        template_path.read_text(encoding="utf-8"),
        existing,
        new_postgres_volume=not _postgres_volume_exists(ctx),
        overrides={k: str(v) for k, v in overrides.items()},
        prompt=ask if ctx.interactive else None,
    )
    renamed = migrated is not None and migrated.changed
    if result.changed or renamed:
        local_env.write_env_file(ctx.env_file, result.text)
    ctx.refresh_values()
    if migrated is not None and migrated.changed:
        ctx.echo(
            f"    renamed {len(migrated.renamed)} variable(s) to the current "
            "names (values kept; see README, 'Environment variables')"
        )
        for old, new in migrated.renamed:
            ctx.echo(f"    {old} -> {new}")
        for old, new in migrated.conflicts:
            ctx.echo(
                f"    conflict: {old} and {new} were both set; kept {new}, "
                f"commented out {old} (check which value you want)"
            )
    shell_legacy = local_env.legacy_in(os.environ)
    if shell_legacy:
        ctx.echo(
            "    warning: your shell exports older variable names the application "
            "refuses: " + ", ".join(shell_legacy) + ". Unset them or export the "
            "new names (README, 'Environment variables')."
        )
    for report in result.reports:
        ctx.echo(f"    {report.key}: {report.render()}")
    for warning in result.warnings:
        ctx.echo(f"    warning: {warning}")
    configured = ctx.values.get(local_env.EXECUTION_BACKEND_KEY, "")
    if ctx.execution_backend and configured and configured != ctx.execution_backend:
        ctx.echo(
            f"    note: this run uses {ctx.execution_backend} execution "
            f"(--execution-backend); the environment file keeps {configured}. "
            f"Edit {local_env.EXECUTION_BACKEND_KEY} there to switch for good."
        )
    created = "created" if existing is None else "updated"
    return StepResult(
        "done",
        f"{created} environment file" if result.changed or renamed else "no changes",
    )


def step_docker(ctx: SetupContext) -> StepResult:
    if shutil.which("docker") is None:
        raise StepFailed(
            "Docker is not installed. Install Docker Desktop (or Docker Engine "
            "with the Compose v2 plugin) and run this command again."
        )
    ctx.run(["docker", "info"], timeout=60)
    ctx.run(["docker", "compose", "version"], timeout=60)
    return StepResult("done", "Docker and Compose are available")


def selected_backend(ctx: SetupContext) -> str:
    """``local`` or ``temporal``: the option, else the file, else ``local``."""
    chosen = (
        ctx.execution_backend
        or ctx.values.get(local_env.EXECUTION_BACKEND_KEY)
        or local_env.DEFAULT_EXECUTION_BACKEND
    )
    if chosen not in local_env.EXECUTION_BACKENDS:
        raise StepFailed(
            f"{local_env.EXECUTION_BACKEND_KEY} must be one of "
            + ", ".join(local_env.EXECUTION_BACKENDS)
        )
    return chosen


def uses_temporal(ctx: SetupContext) -> bool:
    return selected_backend(ctx) == "temporal"


def step_services(ctx: SetupContext) -> StepResult:
    if not uses_temporal(ctx):
        ctx.compose("up", "-d", "--wait", "postgres")
        return StepResult("done", "postgres is healthy (local execution: no Temporal)")
    ctx.compose("up", "-d", "--wait", "postgres", "temporal")
    ctx.compose("run", "--rm", "temporal-namespace")
    return StepResult("done", "postgres and temporal are healthy (Temporal execution)")


GRAFANA_PORT_KEY = "COMPOSE_GRAFANA_PORT"
MLFLOW_PORT_KEY = "COMPOSE_MLFLOW_PORT"


def telemetry_urls(ctx: SetupContext) -> dict[str, str]:
    """Where the local Grafana and MLflow listen (compose defaults, or the file's)."""

    def port(key: str, default: str) -> str:
        # Compose gives the shell environment precedence over the env file.
        return os.environ.get(key) or ctx.values.get(key) or default

    grafana = port(GRAFANA_PORT_KEY, "53000")
    mlflow = port(MLFLOW_PORT_KEY, "55500")
    return {
        "Grafana": f"http://127.0.0.1:{grafana}",
        "MLflow": f"http://127.0.0.1:{mlflow}",
    }


def telemetry_wanted(ctx: SetupContext) -> bool:
    """Start the stack unless opted out or the environment file turns it off."""
    explicit = ctx.values.get(local_env.TELEMETRY_ENABLED_KEY, "").lower()
    return ctx.telemetry and explicit not in {"false", "0", "no", "off"}


def step_telemetry(ctx: SetupContext) -> StepResult:
    ctx.compose("up", "-d", "--build", "--wait", "mlflow", "prometheus", "grafana")
    return StepResult("done", "mlflow, prometheus and grafana are healthy")


def step_migrate(ctx: SetupContext) -> StepResult:
    ctx.python("-m", "alembic", "upgrade", "head")
    return StepResult("done", "database is at the latest revision")


def step_executives(ctx: SetupContext) -> StepResult:
    ctx.python("-m", "retail_analytics.bootstrap.dev_access", "provision", show=True)
    return StepResult("done", "local admin and demo executives provisioned")


def step_golden_seeds(ctx: SetupContext) -> StepResult:
    ctx.python("-m", "retail_analytics.bootstrap.seed_knowledge", show=True)
    return StepResult("done", "Golden knowledge seeded")


def step_golden_embeddings(ctx: SetupContext) -> StepResult:
    ctx.python("-m", "retail_analytics.bootstrap.warm_embeddings", show=True)
    return StepResult("done", "Golden embeddings stored")


def step_check_config(ctx: SetupContext) -> StepResult:
    ctx.python("-m", "retail_analytics.bootstrap.api", "--check-config")
    return StepResult("done", "configuration is valid")


def _has_external_credentials(ctx: SetupContext) -> bool:
    return all(ctx.values.get(key) for key in _CREDENTIAL_KEYS)


_CREDENTIAL_KEYS = (local_env.BIGQUERY_PROJECT_KEY, local_env.GEMINI_API_KEY_KEY)


def step_check_credentials(ctx: SetupContext) -> StepResult:
    if not _has_external_credentials(ctx):
        return StepResult(
            "skipped",
            "BigQuery project or Gemini key not set; fixture mode works without "
            "them. To go live follow docs/google-access.md, then rerun this command",
        )
    ctx.python("-m", "retail_analytics.bootstrap.check_credentials", show=True)
    return StepResult("done", "external credentials verified")


# The single ordered list of bootstrap steps. Later features append their own.
STEPS: tuple[BootstrapStep, ...] = (
    BootstrapStep(
        "environment", "create/complete the environment file", step_environment
    ),
    BootstrapStep("docker", "check Docker and Compose", step_docker),
    BootstrapStep(
        "services",
        "start postgres (and temporal with Temporal execution)",
        step_services,
    ),
    BootstrapStep(
        "telemetry",
        "start mlflow, prometheus and grafana (on by default; --no-telemetry skips)",
        step_telemetry,
        enabled=telemetry_wanted,
    ),
    BootstrapStep("migrate", "apply database migrations", step_migrate),
    BootstrapStep(
        "executives",
        "provision the local admin and the demo executives",
        step_executives,
    ),
    BootstrapStep(
        "golden-seeds", "seed the Golden knowledge library", step_golden_seeds
    ),
    BootstrapStep(
        "golden-embeddings",
        "store Golden embeddings in postgres",
        step_golden_embeddings,
        required=False,
    ),
    BootstrapStep("check-config", "validate the configuration", step_check_config),
    BootstrapStep(
        "check-credentials",
        "verify BigQuery and Gemini access when configured",
        step_check_credentials,
        required=False,
    ),
)


def run_steps(
    ctx: SetupContext, steps: Sequence[BootstrapStep] = STEPS
) -> list[tuple[str, StepResult]]:
    """Run ``steps`` in order. Raises ``StepFailed`` at the first required failure."""
    selected = [step for step in steps if step.enabled(ctx)]
    outcomes: list[tuple[str, StepResult]] = []
    for number, step in enumerate(selected, start=1):
        ctx.echo(f"[{number}/{len(selected)}] {step.name}: {step.summary}")
        try:
            result = step.run(ctx)
        except StepFailed as exc:
            if step.required:
                ctx.echo(f"  failed: {exc}")
                raise
            result = StepResult("warning", str(exc))
        ctx.echo(
            f"  {result.status}" + (f": {result.message}" if result.message else "")
        )
        outcomes.append((step.name, result))
    return outcomes


def next_steps(ctx: SetupContext) -> list[str]:
    lines = ["Local environment is ready (fixture mode works offline)."]
    custom = ctx.env_file.resolve() != (ctx.root / ".env").resolve()
    dev = "./scripts/dev.sh"
    issue_cmd = "retail-analytics-dev-access token local-admin"
    if custom:
        dev += f" --env-file {ctx.env_file}"
        issue_cmd = f"{ENV_FILE_VARIABLE}={ctx.env_file} {issue_cmd}"
    temporal = uses_temporal(ctx)
    if temporal and ctx.execution_backend:
        dev += " --execution-backend temporal"
    lines += [
        "Execution: Temporal (opt-in): durable workflows run by "
        "retail-analytics-worker."
        if temporal
        else "Execution: local (default): investigations run inside the API "
        "process; stopping the API interrupts them (no Temporal, no worker)."
    ]
    if telemetry_wanted(ctx):
        lines += [
            "Telemetry is on: "
            + "  ".join(f"{name} {url}" for name, url in telemetry_urls(ctx).items())
        ]
    lines += [
        "Next:",
        f"  {dev}",
        "      # worker + API together, prefixed logs, Ctrl-C stops both"
        if temporal
        else "      # the API (hosting the investigations), Ctrl-C stops it",
        "  then, in another terminal (after: source .venv/bin/activate):",
        f"  (umask 077; {issue_cmd} > ~/.analytics-token)",
        "      # the local admin's dev token, written to a private file only",
        "  export CLI_TOKEN_FILE=~/.analytics-token",
        "  analytics chat                             # ask a question",
        "  ./scripts/bootstrap.sh                     # rerun any time (idempotent)",
        "  (the production design runs the API and Temporal workers as "
        "separate services; see README, 'Temporal execution')",
    ]
    return lines


@click.command()
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Environment file to create or complete (default: .env in the repository).",
)
@click.option(
    "--project",
    default=None,
    help=f"Compose project name (default: COMPOSE_PROJECT_NAME or {DEFAULT_PROJECT}).",
)
@click.option("--postgres-port", help="Host port for PostgreSQL (new env file only).")
@click.option("--temporal-port", help="Host port for Temporal (new env file only).")
@click.option(
    "--execution-backend",
    type=click.Choice(local_env.EXECUTION_BACKENDS),
    default=None,
    help=(
        "Where investigations execute for this run: local (default) or temporal "
        "(also starts Temporal). Written to the env file only when the key is new."
    ),
)
@click.option(
    "--telemetry/--no-telemetry",
    default=True,
    help=(
        "Start mlflow, prometheus and grafana (the default; --telemetry is "
        "accepted for compatibility). --no-telemetry skips them and writes "
        "TELEMETRY_ENABLED=false to a new key."
    ),
)
@click.option(
    "--interactive",
    is_flag=True,
    help="Prompt (hidden input) for missing external credentials.",
)
@click.option("--env-only", is_flag=True, help="Only create/complete the env file.")
@click.option("--list-steps", is_flag=True, help="Print the ordered steps and exit.")
def main(
    env_file: Path | None,
    project: str | None,
    postgres_port: str | None,
    temporal_port: str | None,
    execution_backend: str | None,
    telemetry: bool,
    interactive: bool,
    env_only: bool,
    list_steps: bool,
) -> None:
    """Set up and seed a working local environment (idempotent)."""
    if list_steps:
        for step in STEPS:
            click.echo(f"{step.name}: {step.summary}")
        return
    ctx = SetupContext(
        root=ROOT,
        env_file=(env_file or ROOT / ".env").resolve(),
        project=project or os.environ.get("COMPOSE_PROJECT_NAME") or DEFAULT_PROJECT,
        telemetry=telemetry,
        interactive=interactive,
        postgres_port=postgres_port,
        temporal_port=temporal_port,
        execution_backend=execution_backend,
    )
    steps = [s for s in STEPS if s.name == "environment"] if env_only else STEPS
    try:
        outcomes = run_steps(ctx, steps)
    except StepFailed:
        click.echo(
            "Bootstrap stopped. Fix the problem above and rerun; completed steps "
            "are not repeated destructively.",
            err=True,
        )
        raise SystemExit(1) from None
    warnings = [name for name, result in outcomes if result.status == "warning"]
    if warnings:
        click.echo("Completed with warnings in: " + ", ".join(warnings))
    for line in next_steps(ctx) if not env_only else []:
        click.echo(line)


if __name__ == "__main__":
    main()
