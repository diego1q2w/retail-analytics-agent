"""Real-data conversational benchmark tooling.

``python -m retail_analytics.bootstrap.realdata_benchmark COMMAND``

- ``extract``  copy the sanitized frozen extract out of BigQuery (needs
  credentials; refuses to replace an existing extract without ``--refresh``).
- ``expected`` compute expected values from the extract (offline, DuckDB), both
  reference routes, and write ``expected.json``.
- ``manifest`` generate ``manifest.json`` from the spec and expected values.
- ``verify``   offline integrity check of the committed artifacts: files match
  their digests, expected values and manifest reproduce, privacy scan passes.
  This is the only check that gates the benchmark.
- ``drift``    re-run the reference SQL on the live warehouse and write a
  separate drift report (never a benchmark verdict; exit code 0 either way).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from retail_analytics.adapters.evaluation import realdata_files as files
from retail_analytics.application.evaluation.realdata import (
    EXTRACT_COLUMNS,
    ExtractManifest,
    assert_publishable,
    build_manifest,
    compute_expected,
    compute_extract_digest,
    drift_report,
    spec_digest,
)

EXIT_PROBLEM = 1
EXIT_MISSING_CONFIG = 2
EXTRACTOR_ID = "retail_analytics.bootstrap.realdata_benchmark/1"
DEFAULT_REPORT = Path("evaluation-results/realdata-drift.json")


def _clients() -> tuple[object, str, str] | None:
    from retail_analytics.adapters.google_access import create_bigquery_client
    from retail_analytics.bootstrap.config import load_backend_settings

    settings = load_backend_settings()
    if settings.bigquery_project is None:
        print("GOOGLE_CLOUD_PROJECT is not set", file=sys.stderr)
        return None
    client = create_bigquery_client(
        settings.bigquery_project, settings.bigquery_location
    )
    return client, settings.bigquery_project, settings.bigquery_location


def cmd_extract(root: Path, refresh: bool) -> int:
    from retail_analytics.adapters.bigquery.realdata_extract import (
        SANITIZATION,
        BigQueryExtractor,
    )

    target = root / files.EXTRACT_DIR
    if (target / files.EXTRACT_MANIFEST).exists() and not refresh:
        print(
            "an extract exists; pass --refresh to replace it (new data version)",
            file=sys.stderr,
        )
        return EXIT_PROBLEM
    spec = files.load_spec(root)
    live = _clients()
    if live is None:
        return EXIT_MISSING_CONFIG
    client, project, location = live
    extractor = BigQueryExtractor(client, project, location)  # type: ignore[arg-type]
    entries, queries = [], []
    for table in ("orders", "order_items", "users", "products"):
        rows, record = extractor.extract_table(spec, table)
        entries.append(
            files.write_table_csv(target, table, EXTRACT_COLUMNS[table], rows)
        )
        queries.append(record)
        print(f"{table}: {len(rows)} rows, {record.bytes_billed} bytes billed")
    manifest = ExtractManifest(
        data_ref=spec.data_ref,
        extract_version=spec.benchmark_version,
        extracted_at=extractor.now(),
        extractor=EXTRACTOR_ID,
        source_dataset=spec.extract.source_dataset,
        location=location,
        window_start=spec.extract.window_start,
        window_end_exclusive=spec.extract.window_end_exclusive,
        date_basis=spec.extract.date_basis,
        sanitization=SANITIZATION,
        source_tables=extractor.source_tables(spec),
        files=tuple(entries),
        queries=tuple(queries),
        extract_digest="0" * 64,
    )
    manifest = manifest.model_copy(
        update={"extract_digest": compute_extract_digest(manifest)}
    )
    files.write_text_atomic(target / files.EXTRACT_MANIFEST, files.dump_json(manifest))
    print(f"extract digest {manifest.extract_digest}")
    return 0


def cmd_expected(root: Path) -> int:
    from retail_analytics.adapters.evaluation.realdata_duckdb import DuckDbExtractEngine

    spec = files.load_spec(root)
    manifest = files.load_extract_manifest(root)
    problems = files.verify_extract_files(root, manifest)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return EXIT_PROBLEM
    engine = DuckDbExtractEngine(root)
    expected = compute_expected(
        spec,
        engine,
        dataset_ref=engine.dataset_ref,
        extract_digest=manifest.extract_digest,
        computed_with="duckdb over the frozen extract",
    )
    files.write_text_atomic(root / files.EXPECTED_FILE, files.dump_json(expected))
    print(f"wrote {files.EXPECTED_FILE} for extract {manifest.extract_digest[:12]}")
    return 0


def cmd_manifest(root: Path) -> int:
    spec = files.load_spec(root)
    manifest = build_manifest(spec, files.load_expected(root))
    files.write_text_atomic(root / files.MANIFEST_FILE, files.dump_json(manifest))
    print(f"wrote {files.MANIFEST_FILE} ({len(manifest.scenarios)} scenarios)")
    return 0


def verify(root: Path) -> list[str]:
    """Problems with the committed artifacts; empty means the benchmark is intact."""
    from retail_analytics.adapters.evaluation.realdata_duckdb import DuckDbExtractEngine

    spec = files.load_spec(root)
    extract = files.load_extract_manifest(root)
    problems = files.verify_extract_files(root, extract)
    if extract.extract_digest != compute_extract_digest(extract):
        problems.append("extract digest does not match the extract manifest")
    if problems:
        return problems
    expected = files.load_expected(root)
    if expected.extract_digest != extract.extract_digest:
        problems.append("expected values belong to a different extract")
    if expected.spec_digest != spec_digest(spec):
        problems.append("expected values belong to a different spec")
    engine = DuckDbExtractEngine(root)
    fresh = compute_expected(
        spec,
        engine,
        dataset_ref=engine.dataset_ref,
        extract_digest=extract.extract_digest,
        computed_with=expected.computed_with,
    )
    if fresh != expected:
        problems.append("expected values do not reproduce from the extract")
    generated = files.dump_json(build_manifest(spec, expected))
    committed = (root / files.MANIFEST_FILE).read_text(encoding="utf-8")
    if generated != committed:
        problems.append("manifest.json differs from the one generated from the spec")
    for path in (
        root / files.EXTRACT_DIR / files.EXTRACT_MANIFEST,
        root / files.EXPECTED_FILE,
        root / files.MANIFEST_FILE,
    ):
        try:
            assert_publishable(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.name}: {exc}")
    return problems


def cmd_verify(root: Path) -> int:
    problems = verify(root)
    if problems:
        print(
            "benchmark artifacts are NOT intact:\n- " + "\n- ".join(problems),
            file=sys.stderr,
        )
        return EXIT_PROBLEM
    print("benchmark artifacts are intact and reproduce from the frozen extract")
    return 0


def cmd_drift(root: Path, out: Path) -> int:
    from retail_analytics.adapters.bigquery.realdata_extract import (
        BigQueryEngine,
        BigQueryExtractor,
    )

    spec = files.load_spec(root)
    expected = files.load_expected(root)
    live = _clients()
    if live is None:
        return EXIT_MISSING_CONFIG
    client, project, location = live
    engine = BigQueryEngine(client, project, location)  # type: ignore[arg-type]
    report = drift_report(
        spec,
        expected,
        engine,
        dataset_ref=engine.dataset_ref(spec),
        observed_at=BigQueryExtractor.now(),
    )
    files.write_text_atomic(out, files.dump_json(report))
    for entry in report.entries:
        print(f"{entry.query_id}: {entry.status}")
    print(
        (
            "LIVE SOURCE DRIFTED from the frozen extract"
            if report.drifted
            else "live source matches the frozen extract"
        )
        + f" (report: {out}); the benchmark verdict is unaffected"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command", choices=("extract", "expected", "manifest", "verify", "drift")
    )
    parser.add_argument("--dir", type=Path, default=files.DEFAULT_DIR)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    if args.command == "extract":
        return cmd_extract(args.dir, args.refresh)
    if args.command == "expected":
        return cmd_expected(args.dir)
    if args.command == "manifest":
        return cmd_manifest(args.dir)
    if args.command == "verify":
        return cmd_verify(args.dir)
    return cmd_drift(args.dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
