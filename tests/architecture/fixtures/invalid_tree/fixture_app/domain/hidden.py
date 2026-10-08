"""Forbidden imports hidden in TYPE_CHECKING and inside a function."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud import bigquery


def client() -> "bigquery.Client":
    from google.cloud import bigquery

    return bigquery.Client()
