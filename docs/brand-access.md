# Brand-based access

The access rule: one retail company, several executives; each manager owns
one or more brands and may analyze only those brands and the orders and
customers related to them. The CEO sees every brand by an explicit grant, not
because of an admin role.

## How it works

- **Assignment.** `executive_brands` holds each manager's brands (migration
  `0020`). Names are the catalog's exact spelling. Assignments come only from
  operator commands (`retail-analytics-dev-access brands assign|remove`,
  `BrandAccessService` in `application/brand_access.py`), never from model
  output, tokens or chat text.
- **Trusted catalog.** `catalog_product_brands` is a snapshot of
  `products.brand`. `retail-analytics-dev-access sync-brands` reads it with a
  fixed, read-only BigQuery query capped by `maximum_bytes_billed`
  (`adapters/bigquery/product_brands.py`) and replaces the snapshot. Bootstrap
  runs it in live mode as the `brand-catalog` step.
- **Resolution.** The directory read (`adapters/postgres/executives.py`)
  returns the explicit grants (`product_entitlements`) plus the snapshot
  products whose brand equals an assigned brand, in one statement together
  with `authorization_version`. The result is the executive's ordinary
  `ProductScope`.
- **Enforcement.** Brand access adds no second check. The resolved
  `ProductScope` goes through the same path as before: compiler product
  binding (and with it which orders and customers are visible), the result
  privacy boundary, evidence authority stamps and scope snapshots, report
  required-scope coverage, release and replay rechecks, citation rechecks,
  and the schema-context cache keys.

## Rules

- **Union rule: explicit grants are independent of brands.** A manager's
  products are the explicit grants (`product_entitlements`) plus the products
  of the assigned brands. Removing a brand removes only what that brand
  contributed; a product that is also granted explicitly stays. Example:
  `exec-x` has the explicit grant `{101}` and the brand "Levi's" (products
  `{101, 102}`). Resolved scope: `{101, 102}`. After
  `brands remove exec-x "Levi's"` the scope is `{101}`, not empty. To take a
  product away completely, remove its brand assignment and its explicit
  grant. The demo brand managers have no explicit grants, so for them brand
  assignment is the whole scope.
- **Exact match.** No case folding, trimming or fuzzy matching. The catalog
  holds case variants (for example "Hugo Boss" and "HUGO BOSS", 39 such
  groups on 2026-10-09) and related labels ("Calvin Klein" and
  "Calvin Klein Jeans"). Each one is a separate brand and must be assigned
  separately.
- **Unknown brands grant nothing.** `assign` refuses a brand that is not in
  the synced catalog. With `--allow-unmatched` the brand is stored but grants
  nothing until a later sync finds products for it.
- **Products without a brand.** 24 live products have a NULL brand. Products
  with a missing, blank or padded brand are not stored in the snapshot, so no
  brand assignment can grant them. Only an explicit grant can.
- **The admin role grants no data.** `exec-local-admin` (the local CEO
  equivalent) has an explicit grant of every product ID (1–29120). Brand
  managers have no explicit grants.
- **Versioning and audit.** Assigning or removing a brand bumps the
  executive's `authorization_version` and writes `access.brands_changed`
  (brand names, product counts and digests). A sync bumps the version of every
  executive whose resolved products changed and writes
  `access.brand_catalog_changed` for each of them. When the snapshot changed,
  it also writes one `access.brand_catalog_synced` event. Brand changes and
  syncs serialize on one advisory lock, so a change is never resolved against
  a half-applied catalog.

## Lifecycle as implemented

| Event | Effect |
| --- | --- |
| New product in an assigned brand | Not visible until the next `sync-brands` (or bootstrap in live mode). The sync adds it to the snapshot and to the manager's scope. |
| Product moves to another brand | At the next sync the old manager loses it and the new one gains it. |
| Brand assigned or removed | The manager's `authorization_version` is bumped at once; audit event `access.brands_changed`. |
| Sync that changes a manager's products | That manager's `authorization_version` is bumped; audit `access.brand_catalog_changed` (and one `access.brand_catalog_synced` if the snapshot changed). |
| Sync that changes nothing | No version bump, no audit event for managers. |

A version bump has these effects through the existing mechanisms (nothing
brand-specific): authority is re-resolved at the model, tool and release
boundaries, so an open run whose manager lost products is stopped and its
answer is withheld or masked with the access-changed notice instead of being
released under the old scope; evidence stamped with the old version is not
reused; the model context and schema-context cache are rebuilt; saved reports
and citations whose required scope is no longer covered show as access
changed. Report titles are not protected after access narrowing; that
gap is documented and out of scope here.

Timing is not otherwise defined. Nothing runs a sync on a schedule and nobody
is notified: production timing and policy are open (see below).

## Catalog changes

When a product moves to another brand, or a brand gains or loses products, the
change takes effect at the next `sync-brands`. The previous manager loses the
product and the new one gains it, and both get a new authorization version.
From then on the existing mechanisms apply: cached evidence stamped with the
old version is not reused, model context is rebuilt, and saved reports or
citations whose scope is no longer covered show as access changed.

Between syncs the stored snapshot is authoritative. A sync that reads no
branded products at all is refused and the previous snapshot stays in force,
because accepting it would revoke every brand grant. An unreachable warehouse
changes nothing either. A product added to an assigned brand is granted at the
next sync. The explicit all-products grant of `exec-local-admin` does not grow
by itself (see the CEO limitation below).

## Local data sources

| Source | Brands | Effect |
| --- | --- | --- |
| Live BigQuery (`bigquery-public-data.thelook_ecommerce.products`) | yes (2,756 spellings) | `sync-brands` and bootstrap use it |
| Held-out fixture (`evaluation/heldout/fixture`) | yes (5 synthetic brands) | `FixtureWarehouse.read_product_brands` (evaluation and tests) |
| Frozen real-data extract (`evaluation/realdata/extract`) | no (column never extracted) | empty catalog: evaluation uses explicit product grants |

Fixture mode (`APP_MODE=fixture`) has no warehouse, and the real brand names
do not exist in the synthetic data. It is therefore provisioned explicitly:
`provision` assigns the demo managers synthetic brands and `sync-brands` (also
a bootstrap step) reads the held-out fixture's `products.json`
(`HeldoutProductBrands`, no DuckDB needed; run from the repository root).

| Manager | Live brands | Fixture brands | Fixture products |
| --- | --- | --- | --- |
| `demo-a` | Calvin Klein, Levi's | Aster, Birch | 201-204 |
| `demo-b` | Carhartt, Columbia | Cedar, Dune | 205-207 |

"Ember" (product 208) is assigned to nobody. The scopes are disjoint: in the
evaluation fixture `demo-a` gets real sales rows for 201-204 and a query for
Cedar's products is outside its scope; `demo-b` is the mirror. An executive
with an empty scope is refused ("No product data is available to you"), never
answered with empty data. Switching `APP_MODE` and rerunning `provision` plus
`sync-brands` reconciles the assignments. The frozen real-data extract has no
brands; evaluation using it keeps explicit product grants.

## CEO and all-products access (demo limitation)

The CEO equivalent (`exec-local-admin`) holds an explicit list of today's
product IDs (1-29120). That is a snapshot of the demo dataset, not an
"all products" entitlement: products added to the catalog later are not
covered until the list is changed. No wildcard or all-products entitlement
type exists, and this documentation does not add one. How the CEO's access
follows new products is an open lifecycle question.

## Production sign-in and lifecycle policy

The design reuses the retailer's existing OIDC identity provider; no separate
password system is proposed. The CLI uses browser-based sign-in, through device
authorization or an appropriate browser callback flow supported by that provider.
The production token verifier/login integration is not implemented. A successful
sign-in establishes identity, not authority to read brands: roles and effective
brand/product grants remain authoritative in PostgreSQL.

An authorized company administrator manages brand assignments, with audited
changes. The local administrative commands implement assignment changes today;
a production administration interface and organizational owner still need to
be selected. This policy does not introduce automatic identity-provider group
to brand mapping.

New products inherit their assigned brand's access at the next successful
catalog sync. Removing a brand removes access supplied by that grant at the
next authorization boundary, including subsequent reads of historical reports
whose required scope is no longer covered. Independent explicit product grants
still apply; removing one grant does not silently erase another. Saved report
content is not rewritten or deleted by revocation, and previously exported
copies cannot be recalled. The documented report-title and CEO snapshot limits
remain; this policy does not claim they are fixed.

## Open questions (access lifecycle, out of scope)

The policy above is selected for the design. These operational details remain
open; documentation alone does not implement them:

- Which company administrator/team owns assignments and which production tool
  they use. Today only local operator commands exist.
- Whether changes need an approval workflow or a second person.
- Synchronization with the company identity provider or HR directory: who
  creates managers and how their brands follow organizational changes.
- When revocation takes effect, and who is notified. Today a change applies
  at the next authorization check; nobody is notified.
- How often the catalog should be synced, and canonical brand identity (should
  case variants be merged?).
- How the CEO's all-brand grant follows new products (today an explicit list
  of existing product IDs; a permanent all-products entitlement does not
  exist).
