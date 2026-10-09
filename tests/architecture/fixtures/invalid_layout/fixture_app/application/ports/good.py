"""A valid port module: the checks must not flag anything here."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fixture_app.application.contracts import Identifier

if TYPE_CHECKING:
    from fixture_app.application.contracts.bad import Record

OrderId = Identifier | None


class GoodPort(Protocol):
    def fetch(self, order_id: OrderId) -> Record | None:
        """Return the record."""
        ...


class WiderPort(GoodPort, Protocol):
    def store(self, record: Record) -> None: ...
