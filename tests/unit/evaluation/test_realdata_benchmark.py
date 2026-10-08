"""Real-data benchmark: reference SQL, frozen extract, drift and privacy.

The reference statements are first proven on the synthetic held-out fixture,
where the answers are known independently, then used on the frozen extract.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from retail_analytics.adapters.evaluation import realdata_files as files
from retail_analytics.adapters.evaluation.realdata_duckdb import DuckDbExtractEngine
from retail_analytics.application.evaluation.manifest import (
    NumericExpectation,
    TextExpectation,
)
from retail_analytics.application.evaluation.realdata import (
    EXTRACT_COLUMNS,
    BenchmarkSpec,
    EngineResult,
    ExpectedValues,
    RouteDisagreement,
    assert_publishable,
    build_manifest,
    check_table_columns,
    compute_expected,
    compute_extract_digest,
    drift_report,
    normalize,
    render_sql,
    spec_digest,
)
from retail_analytics.application.golden_seed_library import seed_library
from retail_analytics.bootstrap import realdata_benchmark as cli
from tests import heldout_fixture as fx

ROOT = Path(__file__).resolve().parents[3] / "evaluation" / "realdata"
SPEC = files.load_spec(ROOT)


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


# ------------------------------------------------ reference SQL on a known fixture


def fixture_spec() -> BenchmarkSpec:
    """The real spec re-pointed at the held-out fixture's dates and products."""
    raw: dict[str, Any] = SPEC.model_dump(mode="json")
    raw["extract"].update(window_start="2026-08-01", window_end_exclusive="2027-03-01")
    raw["scopes"] = {
        name: {"executive_ref": f"x-{name}", "product_lo": lo, "product_hi": hi}
        for name, (lo, hi) in {"women": (201, 208), "men": (201, 208)}.items()
    }
    months = ["2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01"]
    months += ["2027-01-01", "2027-02-01"]
    for qid, query in raw["queries"].items():
        if qid == "monthly_trend":
            query["params"] = {f"m{i + 1}": m for i, m in enumerate(months)}
        elif "w_mid" in query["params"]:
            query["params"] = {
                "w_start": "2026-08-01",
                "w_mid": "2026-09-01",
                "w_end": "2026-11-01",
            }
        else:
            query["params"] = {"w_start": "2026-08-01", "w_end": "2026-11-01"}
    raw["scenarios"] = raw["scenarios"][:1]
    raw["scenarios"][0]["expectations"] = [
        {"kind": "exact", "name": "f", "value": True}
    ]
    return BenchmarkSpec.model_validate(raw)


@pytest.fixture(scope="module")
def fixture_db() -> Any:
    return fx.database()


class FixtureEngine:
    def __init__(self, db: Any) -> None:
        self.db = db

    def run_one(self, sql: str) -> EngineResult:
        cur = self.db.execute(sql)
        names = [d[0] for d in cur.description]
        return EngineResult(row=dict(zip(names, cur.fetchone(), strict=True)))


def test_both_routes_agree_on_the_known_fixture(fixture_db: Any) -> None:
    spec = fixture_spec()
    expected = compute_expected(
        spec,
        FixtureEngine(fixture_db),
        dataset_ref="thelook",
        extract_digest="0" * 64,
        computed_with="test",
    )
    assert set(expected.queries) == set(spec.queries)


def test_reference_sql_matches_independent_python_over_the_fixture(
    fixture_db: Any,
) -> None:
    spec = fixture_spec()
    expected = compute_expected(
        spec,
        FixtureEngine(fixture_db),
        dataset_ref="thelook",
        extract_digest="0" * 64,
        computed_with="test",
    )
    window = ("2026-08-01", "2026-11-01")
    items = [
        i for i in fx.items() if str(window[0]) <= i.ordered_at.isoformat() < window[1]
    ]
    complete = [i for i in items if i.status == "Complete"]
    head = expected.queries["headline"].values
    assert head["revenue"] == pytest.approx(float(sum(i.amount for i in complete)))
    assert head["completed_items"] == len(complete)
    assert head["distinct_customers"] == len({i.customer_id for i in complete})
    assert head["returned_items"] == sum(1 for i in items if i.status == "Returned")
    assert head["window_items_all_statuses"] == len(items)
    states = {c.customer_id: c.state for c in fx.customers()}
    by_state: dict[str, float] = {}
    for i in complete:
        by_state[states[i.customer_id]] = by_state.get(
            states[i.customer_id], 0.0
        ) + float(i.amount)
    top = sorted(by_state.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    got = expected.queries["state_top3"].values
    assert got["state_1"] == top[0]
    assert got["state_1_revenue"] == pytest.approx(round(top[1], 2))


def test_route_disagreement_is_refused(fixture_db: Any) -> None:
    class Skewed(FixtureEngine):
        calls = 0

        def run_one(self, sql: str) -> EngineResult:
            result = super().run_one(sql)
            Skewed.calls += 1
            if Skewed.calls == 2:  # second statement = the crosscheck route
                row = dict(result.row)
                row["revenue"] = float(row["revenue"]) + 1
                return EngineResult(row=row)
            return result

    with pytest.raises(RouteDisagreement):
        compute_expected(
            fixture_spec(),
            Skewed(fixture_db),
            dataset_ref="thelook",
            extract_digest="0" * 64,
            computed_with="test",
        )


# --------------------------------------------------------------- rendering


def test_rendering_binds_only_dates_and_integers_and_stays_read_only() -> None:
    query = SPEC.queries["state_top3"]
    sql = render_sql(query.primary_sql, SPEC, "state_top3", dataset_ref="thelook")
    assert "{" not in sql and "BETWEEN 1 AND 15989" in sql
    with pytest.raises(ValueError, match="unbound"):
        render_sql("SELECT {nope}", SPEC, "state_top3", dataset_ref="x")
    with pytest.raises(ValueError, match="read-only"):
        render_sql("DELETE FROM t", SPEC, "state_top3", dataset_ref="x")


def test_spec_rejects_windows_outside_the_extract_and_free_text_params() -> None:
    raw = SPEC.model_dump(mode="json")
    raw["queries"]["headline"]["params"]["w_end"] = "2027-01-01"
    with pytest.raises(ValueError, match="outside the extract window"):
        BenchmarkSpec.model_validate(raw)
    raw = SPEC.model_dump(mode="json")
    raw["queries"]["headline"]["params"]["w_end"] = "2025-10-01'; DROP TABLE x"
    with pytest.raises(ValueError):
        BenchmarkSpec.model_validate(raw)


def test_windows_are_closed_and_settled() -> None:
    from datetime import date

    end = date.fromisoformat(SPEC.extract.window_end_exclusive)
    extract = files.load_extract_manifest(ROOT)
    extracted = date.fromisoformat(extract.extracted_at[:10])
    assert (extracted - end).days >= SPEC.extract.min_settled_days
    assert "orders.created_at" in SPEC.extract.date_basis
    for query in SPEC.queries.values():
        assert max(query.params.values()) <= SPEC.extract.window_end_exclusive


def test_reference_sql_uses_order_dates_not_item_dates() -> None:
    for query in SPEC.queries.values():
        for sql in (query.primary_sql, query.crosscheck_sql):
            assert re.search(r"oi\.created_at|order_items\.created_at", sql) is None
            assert "o.created_at" in sql


# ------------------------------------------------------- committed artifacts


def test_committed_artifacts_are_intact_and_reproduce() -> None:
    assert cli.verify(ROOT) == []


def test_extract_is_pseudonymous_and_sanitized() -> None:
    manifest = files.load_extract_manifest(ROOT)
    assert manifest.extract_digest == compute_extract_digest(manifest)
    assert {f.table for f in manifest.files} == set(EXTRACT_COLUMNS)
    for entry in manifest.files:
        path = ROOT / entry.path
        check_table_columns(entry.table, files.table_columns(path))
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        assert len(lines) - 1 == entry.rows
        assert_publishable("\n".join(lines[:1]))
    engine = DuckDbExtractEngine(ROOT)
    db = engine._db
    for table in ("orders", "users"):
        id_col = "order_id" if table == "orders" else "id"
        query = f"SELECT COUNT(DISTINCT {id_col}), MIN({id_col}), MAX({id_col}) "
        n, lo, hi = db.execute(query + f"FROM thelook.{table}").fetchone()
        assert (lo, hi) == (1, n), "ids must be dense pseudonyms 1..N"
    ages = db.execute("SELECT DISTINCT age FROM thelook.users").fetchall()
    assert all(a % 5 == 0 and a <= 90 for (a,) in ages), "ages must be 5-year bands"
    assert "UNKNOWN" not in " ".join(manifest.sanitization)
    # nothing in the extract manifest carries a personal value
    assert_publishable(files.dump_json(manifest))


def test_expected_values_are_tied_to_the_extract_and_spec() -> None:
    expected = files.load_expected(ROOT)
    extract = files.load_extract_manifest(ROOT)
    assert expected.extract_digest == extract.extract_digest
    assert expected.spec_digest == spec_digest(SPEC)
    assert expected.data_ref == SPEC.data_ref == extract.data_ref
    for qid in SPEC.queries:
        assert (
            expected.queries[qid].primary_fingerprint
            != expected.queries[qid].crosscheck_fingerprint
        )


def test_manifest_identifies_data_version_scope_and_blocks_without_runtime() -> None:
    manifest = files.load_manifest_file(ROOT)
    expected = files.load_expected(ROOT)
    assert manifest == build_manifest(SPEC, expected)
    assert manifest.manifest_version.endswith(expected.extract_digest[:8])
    assert len(manifest.scenarios) >= 6
    for scenario in manifest.scenarios:
        assert scenario.fixture_ref == SPEC.data_ref
        assert set(scenario.requires) == {"agent_runtime", "frozen_extract_source"}
        assert scenario.scope.product_scope[0].startswith("products:")
        assert "realdata" in scenario.tags


def test_scenarios_cover_state_product_trend_customer_and_reports() -> None:
    manifest = files.load_manifest_file(ROOT)
    tags = {t for s in manifest.scenarios for t in s.tags}
    assert {"state", "product", "trend", "customers", "report", "contributors"} <= tags
    reports = [s for s in manifest.scenarios if s.judge is not None]
    assert len(reports) >= 2
    for scenario in reports:
        names = {e.name for e in scenario.expectations}
        assert {
            "definition_disclosed",
            "action_items_present",
            "evidence_cited",
        } <= names
    multi_turn = [s for s in manifest.scenarios if len(s.dialogue) > 1]
    assert len(multi_turn) >= 3


def test_every_data_expectation_matches_the_expected_extract_values() -> None:
    manifest = files.load_manifest_file(ROOT)
    expected = files.load_expected(ROOT)
    by_id = {s.id: s for s in SPEC.scenarios}
    checked = 0
    for scenario in manifest.scenarios:
        spec_exps = {e.name: e for e in by_id[scenario.id].expectations}
        for exp in scenario.expectations:
            source = spec_exps[exp.name].source
            if source is None:
                continue
            qid, _, column = source.partition(".")
            value = expected.queries[qid].values[column]
            if isinstance(exp, NumericExpectation):
                assert exp.expected == pytest.approx(float(value or 0))
            elif isinstance(exp, TextExpectation):
                assert exp.needle == str(value)
            else:
                assert exp.expected == value  # type: ignore[union-attr]
            checked += 1
    assert checked > 40


def test_heldout_and_seed_material_stay_separate() -> None:
    manifest = files.load_manifest_file(ROOT)
    seeds = seed_library()
    seed_words = [words(s.question) for s in seeds]
    seed_keys = {s.key for s in seeds}
    heldout = json.loads(fx.SPLITS_PATH.read_text(encoding="utf-8"))
    splits = heldout["realdata"]
    assert splits["scenario_ids"] == [s.id for s in manifest.scenarios]
    assert splits["data_ref"] == SPEC.data_ref
    assert set(splits["scenario_ids"]).isdisjoint(heldout["heldout"]["scenario_ids"])
    for scenario in manifest.scenarios:
        assert not ({scenario.id, *scenario.tags} & seed_keys)
        for turn in scenario.dialogue:
            asked = words(turn.text)
            for other in seed_words:
                assert len(asked & other) / len(asked | other) < 0.6, turn.text
    needles = [
        e.needle
        for s in manifest.scenarios
        for e in s.expectations
        if isinstance(e, TextExpectation)
    ]
    for seed in seeds:
        assert not any(n in seed.report_body for n in needles)


def test_artifacts_hold_no_personal_data() -> None:
    for path in [*ROOT.rglob("*.json"), *ROOT.rglob("*.sql"), ROOT / "README.md"]:
        assert_publishable(path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        assert_publishable('{"email": "a@b.co"}')
    with pytest.raises(ValueError):
        assert_publishable("select first_name from users")
    with pytest.raises(ValueError):
        check_table_columns("users", ["id", "age", "state", "email"])


# ------------------------------------------------------------------- drift


class ShiftedEngine:
    """Live source after a restatement: completed revenue changed slightly."""

    def __init__(
        self, expected: ExpectedValues, spec: BenchmarkSpec, shift: float
    ) -> None:
        self.rows = {
            render_sql(q.primary_sql, spec, qid, dataset_ref="live"): dict(
                expected.queries[qid].values
            )
            for qid, q in spec.queries.items()
        }
        self.shift = shift

    def run_one(self, sql: str) -> EngineResult:
        row: Mapping[str, Any] = self.rows[sql]
        out = {
            k: (v + self.shift if k == "total_revenue" and isinstance(v, float) else v)
            for k, v in row.items()
        }
        return EngineResult(row=out, bytes_processed=10)


def test_drift_report_is_separate_and_flags_restatement() -> None:
    expected = files.load_expected(ROOT)
    clean = drift_report(
        SPEC,
        expected,
        ShiftedEngine(expected, SPEC, 0.0),
        dataset_ref="live",
        observed_at="t",
    )
    assert not clean.drifted and clean.kind == "live_drift_report"
    moved = drift_report(
        SPEC,
        expected,
        ShiftedEngine(expected, SPEC, 25.0),
        dataset_ref="live",
        observed_at="t",
    )
    assert moved.drifted
    assert any("total_revenue" in d for e in moved.entries for d in e.differences)
    # a drift report changes neither the expected values nor the manifest
    assert files.load_expected(ROOT) == expected
    assert files.load_manifest_file(ROOT) == build_manifest(SPEC, expected)


def test_drift_errors_are_reported_not_raised() -> None:
    class Broken:
        def run_one(self, sql: str) -> EngineResult:
            raise RuntimeError("quota")

    report = drift_report(
        SPEC, files.load_expected(ROOT), Broken(), dataset_ref="live", observed_at="t"
    )
    assert report.drifted and {e.status for e in report.entries} == {"error"}


def test_normalize_handles_decimals_and_rejects_unknown_types() -> None:
    from decimal import Decimal

    assert normalize(Decimal("1.50")) == 1.5
    with pytest.raises(TypeError):
        normalize(object())


# ------------------------------------------------------------------ runner


def test_runner_blocks_without_runtime_and_passes_a_known_good_agent() -> None:
    from retail_analytics.application.evaluation.runner import RunConfig, run_manifest
    from tests.unit.evaluation.test_heldout_conversations import (
        KnownGoodAgent,
        RubricJudge,
    )

    manifest = files.load_manifest_file(ROOT)
    target = KnownGoodAgent(manifest)
    blocked = run_manifest(manifest, target, RunConfig())
    assert {c.status for c in blocked.cases} == {"blocked"}
    ready = RunConfig(
        available_capabilities=frozenset({"agent_runtime", "frozen_extract_source"})
    )
    passed = run_manifest(manifest, target, ready, judges=[RubricJudge()])
    assert {c.status for c in passed.cases} == {"passed"}
    assert passed.verdict == "passed"
