"""Invalid only at import time: a helper it imports loads Temporal."""

from fixture_app.adapters import retries

POLICY = retries.POLICY
