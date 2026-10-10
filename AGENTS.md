# Working in this repository

Instructions for coding agents and contributors. The project is a
conversational retail analytics agent: a CLI talks to a FastAPI backend that
runs one adaptive Pydantic AI agent over guarded BigQuery queries, with
PostgreSQL state, Golden example retrieval and optional Temporal execution.
Start with the [README](README.md) and the
[architecture overview](docs/architecture/README.md).

## Before you commit

- `./scripts/check.sh` must pass: `ruff check`, `ruff format --check`,
  `mypy` (strict) and `pytest` (architecture tests included). Tests run
  offline; tests that need credentials are marked `live` and skip without
  them; Docker-backed tests are marked `docker` and are run separately with
  `python -m pytest -m docker`.
- Never commit credentials, raw query results, private conversation data,
  the `.env` file or anything under `data/`, `planning/` or `resources/`.
- Commit subjects are short and imperative, in the existing style
  (`fix: ...`, `docs: ...`, `chore: ...`, or a task id such as `T26-F7: ...`).

## Layout and dependency direction

```text
src/retail_analytics/
  domain/        business records, value objects, pure policies and transitions
  application/   use cases, guards (authorization, budgets, privacy), ports, contracts
  capabilities/  typed tool handlers registered with the capability registry
  adapters/      PostgreSQL, BigQuery, SQLGlot compiler, model providers, Temporal, telemetry
  interfaces/    HTTP + SSE API and the CLI (parsing and rendering only)
  bootstrap/     settings, composition roots and command entry points
```

Dependencies point inward and `tests/architecture` enforces them:

- `domain` imports only itself and the standard library.
- `application` and `capabilities` import `domain` and the ports in
  `application/ports`. They never import adapters, interfaces, bootstrap,
  Pydantic AI, Temporal, FastAPI, SQLAlchemy, BigQuery clients, model SDKs
  or telemetry libraries.
- `application/ports/<area>.py` holds `Protocol` ports only;
  `application/contracts/<area>.py` holds shared data types only;
  `application/<area>.py` holds the use cases. Neither ports nor contracts
  import service modules.
- Adapters implement `application.ports.*`; `bootstrap` wires them.
- No import-time client construction, environment reads, file access or
  network calls in inner layers. Pass clocks, IDs and configuration
  explicitly.

Exceptions are declared centrally in `tests/architecture/boundaries.py`
with a reason. Do not weaken the checks to make an import pass.

## Rules that must survive every change

These are enforced in application code, never in prompts. A change that
moves one of them into a prompt, a tool argument or model output is wrong.

- **The model proposes; the application decides.** Tool arguments, SQL and
  drafts are untrusted. Tool inputs cannot carry identity, entitlements,
  budgets or approval; the application resolves those on every attempt.
- **Product scope** is resolved server-side on every tool call and bound
  into every relation by the SQL compiler before aggregation. An empty
  scope gets no data. A brand outside the scope is refused, never reported
  as zero.
- **Privacy.** Query results never contain names, contact details, raw
  customer/order/item keys or exact ages; customer demographics are
  aggregate-only. The result boundary and the output privacy gate re-check
  every answer, report, memory entry and progress text.
- **Deletion** can only be proposed by the agent; confirmation is a
  separate authenticated user action with a typed phrase. The model has no
  confirm tool.
- **Evidence, not rows,** flows back to the model. Query results are
  immutable evidence with IDs that answers must cite.
- **Budgets** (active time, estimated model spend, requests, tokens,
  queries, bytes) are persisted per run and checked before every model
  request and query. Do not raise a default to hide inefficiency.
- **Skills** are bundled, versioned guidance plus tools for the same loop.
  They are not agents and grant no permission. A new skill version is a
  new `V<n>` in `application/skill_assets/`, never an edit of a published
  version.
- **Capabilities** are added through the typed registry and a handler
  (`capabilities/`), not through tool-name conditionals. A new tool cannot
  bypass authorization, the output gate or operation recording.
- **Errors** shown to users are sanitized codes and messages from the fixed
  vocabulary in `domain/errors.py`. No secrets or personal data in
  exceptions, logs, telemetry or `repr` output. An infrastructure failure
  must never become successful-looking evidence.

## Code style

- Small, typed functions; `Protocol` ports for real boundaries; composition
  over inheritance; no abstraction for a hypothetical future.
- Keep framework objects (Pydantic AI, SQLAlchemy, SQLGlot, HTTP) out of
  durable business records.
- Comments explain a decision or a constraint, not what the code obviously
  does. Match the density and voice of the surrounding module.
- Tests assert externally meaningful invariants. Use pure tests for
  policies, contract tests for ports (fakes obey the same result, error and
  idempotency contracts as the real adapter), PostgreSQL tests for
  transactions and concurrency.

## Documentation

- Every claim in `docs/architecture` carries a status: implemented,
  measured, pending, proposed or deferred (legend in
  [the overview](docs/architecture/README.md#how-to-read-the-claims)).
  Do not upgrade a status without the code, test or dated result that
  earns it, and do not relabel blocked evaluation results as passed.
- Measured numbers link to the file they come from under `evaluation/`.
- Keep the README short; detail goes to `docs/`.
- Never reference private planning notes or local paths outside the
  repository in public documents, commits or code.

## Useful commands

```sh
./scripts/bootstrap.sh --interactive   # one-time local setup (PostgreSQL, telemetry, seeds)
./scripts/dev.sh                       # start the API (runs investigations in-process)
./scripts/local_cli.sh                 # chat as the local administrator
./scripts/check.sh                     # lint, format, strict typing, tests
python -m pytest tests/architecture    # layer boundary checks only
python -m pytest -m docker             # Docker-backed tests
```

Runtime, API, CLI and evaluation details: [docs/](docs/development.md).
