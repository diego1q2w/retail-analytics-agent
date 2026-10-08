"""Files of the real-data benchmark: spec, frozen extract, expected values.

Layout under ``evaluation/realdata/``: ``spec.json``, ``reference-sql/``,
``extract/`` (gzip CSV per table plus ``extract-manifest.json``),
``expected.json`` and the generated ``manifest.json``.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from retail_analytics.application.evaluation.manifest import Manifest
from retail_analytics.application.evaluation.realdata import (
    BenchmarkSpec,
    ExpectedValues,
    ExtractManifest,
    TableFile,
    assert_publishable,
    check_table_columns,
)

DEFAULT_DIR: Final = Path("evaluation/realdata")
SPEC_FILE: Final = "spec.json"
EXTRACT_DIR: Final = "extract"
EXTRACT_MANIFEST: Final = "extract-manifest.json"
EXPECTED_FILE: Final = "expected.json"
MANIFEST_FILE: Final = "manifest.json"


def load_spec(root: Path = DEFAULT_DIR) -> BenchmarkSpec:
    """Read ``spec.json`` and attach each query's two reference statements."""
    raw: dict[str, Any] = json.loads((root / SPEC_FILE).read_text(encoding="utf-8"))
    for query in raw["queries"].values():
        stem = root / "reference-sql" / query["sql"]
        query["primary_sql"] = stem.with_name(stem.name + ".primary.sql").read_text(
            encoding="utf-8"
        )
        query["crosscheck_sql"] = stem.with_name(
            stem.name + ".crosscheck.sql"
        ).read_text(encoding="utf-8")
    return BenchmarkSpec.model_validate(raw)


def dump_json(model: Any) -> str:
    """Stable, human-diffable JSON for a pydantic model."""
    text = json.dumps(model.model_dump(mode="json"), indent=1, sort_keys=True) + "\n"
    assert_publishable(text)
    return text


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_extract_manifest(root: Path = DEFAULT_DIR) -> ExtractManifest:
    return ExtractManifest.model_validate_json(
        (root / EXTRACT_DIR / EXTRACT_MANIFEST).read_text(encoding="utf-8")
    )


def load_expected(root: Path = DEFAULT_DIR) -> ExpectedValues:
    return ExpectedValues.model_validate_json(
        (root / EXPECTED_FILE).read_text(encoding="utf-8")
    )


def load_manifest_file(root: Path = DEFAULT_DIR) -> Manifest:
    return Manifest.model_validate_json(
        (root / MANIFEST_FILE).read_text(encoding="utf-8")
    )


def write_table_csv(
    directory: Path, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> TableFile:
    """Write one table as deterministic gzip CSV (fixed mtime, sorted by caller)."""
    check_table_columns(table, columns)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    path = directory / f"{table}.csv.gz"
    directory.mkdir(parents=True, exist_ok=True)
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
    ):
        zipped.write(buffer.getvalue().encode("utf-8"))
    return TableFile(
        table=table,
        path=f"{EXTRACT_DIR}/{path.name}",
        rows=len(rows),
        sha256=file_sha256(path),
    )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_extract_files(root: Path, manifest: ExtractManifest) -> list[str]:
    """Problems found between the extract manifest and the files on disk."""
    problems: list[str] = []
    for entry in manifest.files:
        path = root / entry.path
        if not path.exists():
            problems.append(f"{entry.path}: missing")
        elif file_sha256(path) != entry.sha256:
            problems.append(f"{entry.path}: sha256 differs from the manifest")
    return problems


def table_columns(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        return next(csv.reader(handle))


def describe(mapping: Mapping[str, Any]) -> str:
    return json.dumps(mapping, sort_keys=True)
