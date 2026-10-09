"""Service module that defines its own Protocol and re-exports a port."""

from __future__ import annotations

from typing import Protocol

from fixture_app.application.ports.good import GoodPort

NAME = "orders"


class LocalPort(Protocol):
    def fetch(self) -> str: ...


__all__ = ["GoodPort", "NAME"]
