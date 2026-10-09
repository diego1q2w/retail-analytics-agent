"""SQLAlchemy Core table descriptions used by the PostgreSQL adapters.

Migrations own the schema (``migrations/versions``); these descriptions only
name the columns queries use and must stay in step with the latest revision.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = sa.MetaData()


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


sessions = sa.Table(
    "sessions",
    metadata,
    sa.Column("session_id", sa.Text, primary_key=True),
    sa.Column("executive_id", sa.Text, nullable=False),
    _ts("created_at"),
    _ts("last_activity_at"),
)

messages = sa.Table(
    "messages",
    metadata,
    sa.Column("message_id", sa.Text, primary_key=True),
    sa.Column("position", sa.BigInteger, sa.Identity()),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    _ts("created_at"),
)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("requested_by", sa.Text, nullable=False),
    sa.Column("trigger_message_id", sa.Text, nullable=False),
    sa.Column("submission_key", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("temporal_workflow_id", sa.Text),
    sa.Column("temporal_run_id", sa.Text),
    sa.Column("last_event_sequence", sa.Integer, nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
    _ts("completed_at", nullable=True),
)

tool_executions = sa.Table(
    "tool_executions",
    metadata,
    sa.Column("operation_id", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=False),
    sa.Column("capability_version", sa.Integer, nullable=False),
    sa.Column("side_effect", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("attempt_count", sa.Integer, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("error_detail", sa.String(280)),
    _ts("deadline_at", nullable=True),
    _ts("created_at"),
    _ts("updated_at"),
)

query_executions = sa.Table(
    "query_executions",
    metadata,
    sa.Column("operation_id", sa.Text, primary_key=True),
    sa.Column("submission", sa.Integer, primary_key=True),
    sa.Column("job_id", sa.Text, nullable=False),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("location", sa.Text, nullable=False),
    sa.Column("query_fingerprint", sa.Text, nullable=False),
    sa.Column("query_ref", sa.Text, nullable=False),
    sa.Column("authorization_version", sa.Integer, nullable=False),
    sa.Column("catalog_version", sa.Text, nullable=False),
    _ts("registered_at"),
)

execution_events = sa.Table(
    "execution_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("operation_id", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("from_status", sa.Text),
    sa.Column("to_status", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("detail", sa.String(280)),
    _ts("occurred_at"),
)

run_events = sa.Table(
    "run_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    _ts("occurred_at"),
)

executives = sa.Table(
    "executives",
    metadata,
    sa.Column("executive_id", sa.Text, primary_key=True),
    sa.Column("issuer", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("label", sa.String(120), nullable=False),
    sa.Column("roles", ARRAY(sa.Text), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("authorization_version", sa.Integer, nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
)

product_entitlements = sa.Table(
    "product_entitlements",
    metadata,
    sa.Column("executive_id", sa.Text, primary_key=True),
    sa.Column("product_id", sa.Text, primary_key=True),
    _ts("granted_at"),
)

artifact_versions = sa.Table(
    "artifact_versions",
    metadata,
    sa.Column("artifact_id", sa.Text, primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("media_type", sa.Text, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    _ts("created_at"),
)

user_preferences = sa.Table(
    "user_preferences",
    metadata,
    sa.Column("preference_id", sa.Text, primary_key=True),
    sa.Column("executive_id", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text),
    sa.Column("slot", sa.Text, nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
)

preference_proposals = sa.Table(
    "preference_proposals",
    metadata,
    sa.Column("proposal_id", sa.Text, primary_key=True),
    sa.Column("executive_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("slot", sa.Text, nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("observations", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
    _ts("expires_at", nullable=True),
)

preference_events = sa.Table(
    "preference_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("executive_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text),
    sa.Column("slot", sa.Text, nullable=False),
    sa.Column("scope", sa.Text),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("source", sa.Text),
    sa.Column("version", sa.Integer),
    _ts("occurred_at"),
)

golden_versions = sa.Table(
    "golden_versions",
    metadata,
    sa.Column("example_id", sa.String(32), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("author_id", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("restricted_product_ids", ARRAY(sa.Text), nullable=False),
    sa.Column("schema_version", sa.Text, nullable=False),
    sa.Column("metric_refs", JSONB, nullable=False),
    sa.Column("question", sa.Text),
    sa.Column("sql_text", sa.Text),
    sa.Column("method_summary", sa.Text),
    sa.Column("report_artifact_id", sa.Text),
    sa.Column("report_artifact_version", sa.Integer),
    sa.Column("content_digest", sa.String(64)),
    sa.Column("purge_artifact_id", sa.Text),
    _ts("created_at"),
    _ts("status_changed_at"),
    sa.Column("reviewed_by", sa.Text),
    _ts("reviewed_at", nullable=True),
)

golden_provenance = sa.Table(
    "golden_provenance",
    metadata,
    sa.Column("example_id", sa.String(32), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("source_kind", sa.Text, nullable=False),
    sa.Column("source_id", sa.Text),
    sa.Column("source_version", sa.Integer),
)

golden_review_events = sa.Table(
    "golden_review_events",
    metadata,
    sa.Column("event_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("example_id", sa.String(32), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("from_status", sa.Text),
    sa.Column("to_status", sa.Text, nullable=False),
    sa.Column("rationale", sa.Text, nullable=False),
    sa.Column("checks", JSONB),
    _ts("at"),
)

golden_index_events = sa.Table(
    "golden_index_events",
    metadata,
    sa.Column("sequence", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("example_id", sa.String(32), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    _ts("at"),
)

evidence = sa.Table(
    "evidence",
    metadata,
    sa.Column("evidence_id", sa.Text, primary_key=True),
    sa.Column("lineage_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("executive_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("operation_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subject_key", sa.Text, nullable=False),
    sa.Column("authorization_version", sa.Integer, nullable=False),
    sa.Column("scope_digest", sa.String(64), nullable=False),
    sa.Column("catalog_version", sa.Integer, nullable=False),
    sa.Column("policy_version", sa.Integer, nullable=False),
    sa.Column("preference_fingerprint", sa.Text, nullable=False),
    sa.Column("analysis", JSONB, nullable=False),
    sa.Column("provenance", JSONB, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("grain", ARRAY(sa.Text), nullable=False),
    sa.Column("analytical_slots", ARRAY(sa.Text), nullable=False),
    sa.Column("truncated", sa.Boolean, nullable=False),
    sa.Column("content_digest", sa.String(64), nullable=False),
    _ts("computed_at"),
    _ts("recorded_at"),
)

evidence_dependencies = sa.Table(
    "evidence_dependencies",
    metadata,
    sa.Column("evidence_id", sa.Text, primary_key=True),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("depends_on", sa.Text, nullable=False),
)

run_evidence = sa.Table(
    "run_evidence",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("evidence_id", sa.Text, primary_key=True),
    sa.Column("use", sa.Text, nullable=False),
    _ts("linked_at"),
)

evidence_invalidations = sa.Table(
    "evidence_invalidations",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("evidence_id", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("slot", sa.Text),
    _ts("invalidated_at"),
)

evidence_pins = sa.Table(
    "evidence_pins",
    metadata,
    sa.Column("evidence_id", sa.Text, primary_key=True),
    sa.Column("holder_kind", sa.Text, primary_key=True),
    sa.Column("holder_id", sa.Text, primary_key=True),
    _ts("pinned_at"),
)

golden_embeddings = sa.Table(
    "golden_embeddings",
    metadata,
    sa.Column("content_digest", sa.String(64), primary_key=True),
    sa.Column("model_id", sa.Text, primary_key=True),
    sa.Column("dimensions", sa.Integer, primary_key=True),
    sa.Column("vector", ARRAY(sa.Float(53)), nullable=False),
    _ts("created_at"),
)

topic_resets = sa.Table(
    "topic_resets",
    metadata,
    sa.Column("reset_id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, nullable=False),
    _ts("reset_at"),
)

run_budgets = sa.Table(
    "run_budgets",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("limits", JSONB, nullable=False),
    sa.Column("active_seconds_used", sa.Float(53), nullable=False),
    _ts("active_since", nullable=True),
    sa.Column("provider_requests", sa.Integer, nullable=False),
    sa.Column("tokens", sa.BigInteger, nullable=False),
    sa.Column("queries", sa.Integer, nullable=False),
    sa.Column("bytes", sa.BigInteger, nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
)

budget_charges = sa.Table(
    "budget_charges",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, primary_key=True),
    sa.Column("charge_key", sa.Text, primary_key=True),
    sa.Column("group_key", sa.Text),
    sa.Column("bytes", sa.BigInteger, nullable=False),
    sa.Column("tokens", sa.BigInteger, nullable=False),
    sa.Column("settled", sa.Boolean, nullable=False),
    sa.Column("ambiguous", sa.Boolean, nullable=False),
    _ts("created_at"),
    _ts("settled_at", nullable=True),
)

run_principals = sa.Table(
    "run_principals",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("executive_id", sa.Text, nullable=False),
    sa.Column("scopes", ARRAY(sa.Text), nullable=False),
    _ts("recorded_at"),
)

run_inputs = sa.Table(
    "run_inputs",
    metadata,
    sa.Column("input_id", sa.Text, primary_key=True),
    sa.Column("position", sa.BigInteger, sa.Identity()),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("message_id", sa.Text),
    sa.Column("question_id", sa.Text),
    sa.Column("promoted_run_id", sa.Text),
    _ts("created_at"),
    _ts("applied_at", nullable=True),
)

run_questions = sa.Table(
    "run_questions",
    metadata,
    sa.Column("question_id", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("message_id", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    _ts("asked_at"),
    _ts("closed_at", nullable=True),
)

reports = sa.Table(
    "reports",
    metadata,
    sa.Column("report_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text),
    _ts("created_at"),
    _ts("deleted_at", nullable=True),
)

report_versions = sa.Table(
    "report_versions",
    metadata,
    sa.Column("report_id", sa.Text, primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("artifact_version", sa.Integer, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("draft_digest", sa.String(64), nullable=False),
    sa.Column("scope_digest", sa.String(64), nullable=False),
    sa.Column("authorization_version", sa.Integer, nullable=False),
    _ts("created_at"),
)

report_evidence = sa.Table(
    "report_evidence",
    metadata,
    sa.Column("report_id", sa.Text, primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("evidence_id", sa.Text, primary_key=True),
    sa.Column("ordinal", sa.Integer, nullable=False),
)

deletion_proposals = sa.Table(
    "deletion_proposals",
    metadata,
    sa.Column("proposal_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text),
    sa.Column("run_id", sa.Text),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("request_digest", sa.String(64), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    _ts("created_at"),
    _ts("expires_at"),
    _ts("resolved_at", nullable=True),
)

deletion_proposal_items = sa.Table(
    "deletion_proposal_items",
    metadata,
    sa.Column("proposal_id", sa.Text, primary_key=True),
    sa.Column("report_id", sa.Text, primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
)

audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("audit_id", sa.Text, primary_key=True),
    _ts("occurred_at"),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("subject_type", sa.Text, nullable=False),
    sa.Column("subject_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text),
    sa.Column("run_id", sa.Text),
    sa.Column("details", JSONB, nullable=False),
)
