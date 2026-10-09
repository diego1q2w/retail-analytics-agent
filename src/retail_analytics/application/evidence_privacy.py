"""Current privacy policy applied to stored evidence (legacy demographics).

Customer demographics became aggregate-only with result privacy policy
version 2 (``PRIVACY_POLICY_VERSION``). Evidence recorded earlier may hold
individual-level demographics (a customer's state or age band next to its
reference, or rows selected by a reference). Stored evidence is immutable, so
it is never rewritten; instead every use is screened here and an unsafe
record is withheld everywhere evidence is judged (``ReuseBlock.
PRIVACY_POLICY_WITHDRAWN``): model context, ``fetch_evidence``, citations,
chat history and replay, report read/export/search, reuse and imports.

Classification of a record computed before version 2, fail closed:

- external evidence (exchange rates) and query evidence that read no relation
  holding a demographic field: unaffected (safe);
- query evidence that read such a relation: safe only if no column has the
  reference role, no cell has the shape of an opaque reference, and the
  stored logical query passes the current grain check (``QueryGrainAudit``,
  the compiler's own analysis). Without an audit, or when the query cannot be
  verified, it is withheld;
- derived evidence (currency conversions): safe only if every input is safe
  (judged by the service, which can load the inputs).

Records from version 2 on were released by the aggregate-only boundary.
"""

from __future__ import annotations

from enum import StrEnum

from retail_analytics.application.ports.evidence import QueryGrainAudit
from retail_analytics.domain.catalog import LogicalCatalog
from retail_analytics.domain.evidence import Evidence, EvidenceKind
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.privacy import is_reference

# First result-policy version under which demographics are aggregate-only
# (the current ``PRIVACY_POLICY_VERSION`` is this or later).
AGGREGATE_DEMOGRAPHICS_POLICY = 2


class PrivacyVerdict(StrEnum):
    SAFE = "safe"
    WITHDRAWN = "withdrawn"
    # Derived evidence: safe only if all inputs are (the caller decides).
    DEPENDS_ON_INPUTS = "depends_on_inputs"


class EvidencePrivacyScreen:
    def __init__(
        self,
        catalog: LogicalCatalog | None = None,
        audit: QueryGrainAudit | None = None,
    ) -> None:
        catalog = catalog or default_logical_catalog()
        self._audit = audit
        self._demographic_relations = frozenset(
            r.name for r in catalog.relations if any(f.demographic for f in r.fields)
        )

    def verdict(self, evidence: Evidence) -> PrivacyVerdict:
        content = evidence.content
        if content.analysis.policy_version >= AGGREGATE_DEMOGRAPHICS_POLICY:
            return PrivacyVerdict.SAFE
        if content.kind is EvidenceKind.EXTERNAL:
            return PrivacyVerdict.SAFE
        if content.kind is EvidenceKind.DERIVED:
            return PrivacyVerdict.DEPENDS_ON_INPUTS
        table = content.table
        sources = {s for c in table.columns for s in c.sources}
        relations = set(content.provenance.relations) | {
            s.split(".", 1)[0] for s in sources
        }
        if not relations & self._demographic_relations:
            return PrivacyVerdict.SAFE
        if any(c.role == "reference" for c in table.columns):
            return PrivacyVerdict.WITHDRAWN
        if any(is_reference(cell) for row in table.rows for cell in row):
            return PrivacyVerdict.WITHDRAWN
        sql = content.provenance.logical_sql
        if self._audit is None or not sql:
            return PrivacyVerdict.WITHDRAWN
        try:
            safe = self._audit.aggregate_only(sql)
        except Exception:
            safe = False
        return PrivacyVerdict.SAFE if safe is True else PrivacyVerdict.WITHDRAWN
