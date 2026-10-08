"""Reverse dependency: domain reaching out to a concrete adapter."""

from fixture_app.adapters import store

USED = store.STORE
