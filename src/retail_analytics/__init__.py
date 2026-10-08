"""Retail analytics assistant.

Layers (dependencies point inward; enforced by tests/architecture):

- domain: business records, value objects and pure policies (stdlib only)
- application: use cases, gates and the narrow ports they own
- capabilities: typed analytical/report/knowledge capability handlers
- adapters: PostgreSQL, BigQuery, SQLGlot, models, Temporal, artifacts, telemetry
- interfaces: HTTP/SSE and CLI translation of requests and results
- bootstrap: configuration loading and composition roots (API, worker, CLI)
"""

__version__ = "0.1.0"
