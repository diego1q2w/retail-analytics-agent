"""Live BigQuery dry runs of every Golden seed query (free; no rows are read).

Complements the fixture checks: the compiled form of each seed step must be
accepted by BigQuery with its typed parameters. Skipped without a configured
project and credentials.
"""

from __future__ import annotations

import pytest
from google.cloud import bigquery

from retail_analytics.application.golden_seed_library import SeedExample, seed_library
from tests.live.test_sql_compiler_dry_run import LIVE_SCOPE, _dry_run, client
from tests.unit.sql_compiler.support import compile_sql

pytestmark = pytest.mark.live

__all__ = ["client"]

CASES = [(e, n) for e in seed_library() for n in range(len(e.steps))]


@pytest.mark.parametrize(
    ("example", "step_index"), CASES, ids=[f"{e.key}-{n}" for e, n in CASES]
)
def test_seed_step_passes_bigquery_dry_run(
    client: bigquery.Client, example: SeedExample, step_index: int
) -> None:
    step = example.steps[step_index]
    compiled = compile_sql(step.sql, LIVE_SCOPE, step.parameters)
    scanned = _dry_run(client, compiled)
    assert 0 <= scanned <= compiled.maximum_bytes_billed
