"""Profile the live public source tables and write a sanitized report.

``python -m retail_analytics.bootstrap.profile_source [--out DIR]``

Needs RETAIL_ANALYTICS_BIGQUERY_PROJECT and application default credentials.
Writes ``source-profile.json`` and ``source-profile.md`` (aggregates only).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from retail_analytics.adapters.bigquery.source_profile import (
    assert_sanitized,
    build_report,
    render_markdown,
)
from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.bootstrap.config import load_backend_settings

DEFAULT_OUT = Path("docs/source-profile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    settings = load_backend_settings()
    if settings.bigquery_project is None:
        print("RETAIL_ANALYTICS_BIGQUERY_PROJECT is not set", file=sys.stderr)
        return 2
    client = create_bigquery_client(
        settings.bigquery_project, settings.bigquery_location
    )
    report = build_report(settings.bigquery_project, settings.bigquery_location, client)
    markdown = render_markdown(report)
    assert_sanitized(markdown)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "source-profile.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    (args.out / "source-profile.md").write_text(markdown)
    print(f"wrote {args.out}/source-profile.json and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
