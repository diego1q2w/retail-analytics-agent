"""Composition root may import everything: no violation expected here."""

from fixture_app.adapters import store
from fixture_app.domain import reverse

WIRED = (store, reverse)
