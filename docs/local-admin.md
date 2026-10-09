# Local administration (optional)

Nothing on this page is needed to ask questions, save reports or delete them
(see the [README quick start](../README.md#quick-start)). It covers the identities bootstrap creates and
the optional admin actions: switching to a restricted identity, publishing a
Golden example and changing the persona. All commands run with the backend's
own settings from the repository root, inside the virtualenv
(`source .venv/bin/activate`), and print no secrets.

## Identities

`./scripts/bootstrap.sh` runs `retail-analytics-dev-access provision`, which
creates or reconciles these synthetic identities (safe to rerun; every change
is audited with actor `system:dev-access`):

| Token name | Executive ID | Roles | Products |
| --- | --- | --- | --- |
| `local-admin` | `exec-local-admin` | executive, editor, reviewer, admin | 1–29120 (every product), granted explicitly |
| `demo-a` | `exec-demo-a` | executive, editor | brands "Calvin Klein", "Levi's" |
| `demo-b` | `exec-demo-b` | executive, reviewer | brands "Carhartt", "Columbia" |

You are `local-admin`. Roles grant operations; products are a separate
explicit grant, so the admin role by itself gives no product data. The two
restricted identities are brand managers for showing authorization: their
brands resolve to products through the brand catalog that bootstrap syncs from
BigQuery in live mode (`retail-analytics-dev-access sync-brands`). In fixture
mode the same step reads the synthetic fixture catalog and the managers get
other brands: `demo-a` Aster and Birch, `demo-b` Cedar and Dune. Their data
never overlaps, and neither can read the other's reports. The `local-admin`
grant is an explicit list of today's product IDs, so products added later are
not covered until it is changed.

Change a manager's brands (exact catalog spelling; each change bumps the
authorization version and is audited):

```sh
retail-analytics-dev-access brands list demo-a
retail-analytics-dev-access brands assign demo-a "Tommy Hilfiger"
retail-analytics-dev-access brands remove demo-a "Levi's"
```

A brand change only affects what the brand contributes: products that are
also granted explicitly stay (explicit grants and brands are added together).
New products of an assigned brand appear after the next `sync-brands`; every
change that alters a manager's products bumps the authorization version and
is audited. See [brand-based access](brand-access.md) for the rules, the
lifecycle as implemented and the open questions.

```sh
(umask 077; retail-analytics-dev-access token demo-b > ~/.analytics-token-b)
CLI_TOKEN_FILE=~/.analytics-token-b analytics chat
```

## Tokens by hand

`./scripts/local_cli.sh` issues a local-admin token and opens the chat in one
step. To do the same by hand, for example with another token file or a longer
lifetime (`--minutes`, up to 1440):

```sh
source .venv/bin/activate
(umask 077; retail-analytics-dev-access token local-admin > ~/.analytics-token)
export CLI_TOKEN_FILE=~/.analytics-token
analytics chat
```

The token is written only to that file and lasts 60 minutes by default.
`umask` protects a new file only; an existing file keeps its permissions
(the launcher sets mode 0600 itself).

## Publishing a Golden example

Golden examples are reviewed question/SQL/report trios that the assistant
retrieves as worked methods. Bootstrap already publishes the project's seed
library (submitted by `demo-a`, approved by `demo-b`). To add your own:

```json
{
  "question": "How did revenue change month by month over the last three complete months?",
  "sql": "SELECT DATE_TRUNC(ordered_date, MONTH) AS month, SUM(sale_amount) AS completed_item_sales FROM sales_items WHERE item_status = 'Complete' AND ordered_date >= @window_start AND ordered_date < @window_end GROUP BY month ORDER BY month",
  "method_summary": "Revenue is completed_item_sales v1 over complete calendar months (UTC, half-open window). State the definition and window.",
  "report_markdown": "# Monthly revenue\n\nCompare complete months only; say that only permitted products are included.",
  "metrics": [{"metric_id": "completed_item_sales", "version": 1}],
  "sanitization_attested": true
}
```

```sh
retail-analytics-knowledge submit --as exec-local-admin --key my-first-example --file example.json
#   example=<example-id> version=1 status=candidate
retail-analytics-knowledge queue --as exec-local-admin
retail-analytics-knowledge show <example-id> 1 --as exec-local-admin
retail-analytics-knowledge approve <example-id> 1 --as exec-local-admin \
    --rationale "Checked the SQL, definition and wording." --correct --sanitized --applicable
#   ... status=published review=self-published (local policy; not independent review)
retail-analytics-knowledge history <example-id> 1 --as exec-local-admin
```

Optional fields: `schema_version` (default: the current logical catalog),
`restricted_product_ids` (default: shared with everyone) and `origin`
(default `project_authored`). Submission screens for personal data and
identifier lists; approval needs all three checks.

**Self-publication policy (local development only).** Normally the reviewer
who approves, rejects or reinstates an example must be someone other than its
author. For the local demo, the user authorized one exception: the
`retail-analytics-knowledge` command lets `exec-local-admin`, and no one
else, approve their own example, so you need no second identity. It is not
independent review: the review event records `self_published=true` with you
as both author and reviewer, and `history` shows it. Everything else still
applies: your current server-side roles and product scope are checked on
every command, content is screened, and authorship is never rewritten. Other
executives, including `demo-b` (a reviewer), still get `self_review` when they
try to approve their own example. The API, the seed command and the agent
never use this policy. The production design keeps independent review
([architecture](architecture/README.md)).

## Changing the persona

The persona is presentation-only text (tone, layout, terminology). Any editor
can publish a previewed draft, so the local admin needs no second identity
here either; the history records who published.

```sh
retail-analytics-persona draft --as exec-local-admin --key style-1 --text "Lead with the headline number; keep it short."
retail-analytics-persona preview <draft-id> --as exec-local-admin
retail-analytics-persona publish <draft-id> --as exec-local-admin
retail-analytics-persona history --as exec-local-admin
```

To go back to an earlier published version:
`retail-analytics-persona rollback <version-id> --as exec-local-admin` (IDs are in `history`). If
publish says the persona changed, run `preview` again. New runs use the published version; see
[persona management](components.md#persona-management).

## What stays human-only

Deleting reports always needs your typed confirmation in the CLI
(`/confirm <proposal-id>` or `analytics deletion confirm`). The model can only
propose a deletion, and no admin role or setting changes that.

Restoring a deleted report within seven days and the cleanup job are operator
commands (`retail-analytics-maintenance`; see [report recovery and lifecycle
cleanup](components.md#report-recovery-and-lifecycle-cleanup)).
