# Brand-based access

The client's rule: one retail company, several executives; each manager owns
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
by itself.

## Local data sources

| Source | Brands | Effect |
| --- | --- | --- |
| Live BigQuery (`bigquery-public-data.thelook_ecommerce.products`) | yes (2,756 spellings) | `sync-brands` and bootstrap use it |
| Held-out fixture (`evaluation/heldout/fixture`) | yes (5 synthetic brands) | `FixtureWarehouse.read_product_brands` (evaluation and tests) |
| Frozen real-data extract (`evaluation/realdata/extract`) | no (column never extracted) | empty catalog: evaluation uses explicit product grants |

Fixture mode (`APP_MODE=fixture`) has no warehouse. The bootstrap step is
skipped there, and the demo brand managers see no products.

## Open questions (access lifecycle, out of scope)

The client confirmed brand-based access. The lifecycle questions below are
still open and nothing here implements them:

- Who administers brand assignments in production, and through which tool.
  Today only local operator commands exist.
- Whether changes need an approval workflow or a second person.
- Synchronization with the company identity provider or HR directory: who
  creates managers and how their brands follow organizational changes.
- When revocation takes effect, and who is notified. Today a change applies
  at the next authorization check; nobody is notified.
- Whether products newly added to an assigned brand, or moved into it, should
  be granted automatically at the next sync, as they are now, or only after
  review. Also whether reports saved before a reassignment stay readable;
  today the existing required-scope rule applies.
- How often the catalog should be synced, and canonical brand identity (should
  case variants be merged?).
- How the CEO's all-brand grant follows new products.
