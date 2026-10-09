"""Contracts module that holds interfaces and services (deliberately)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fixture_app.adapters import impl
from fixture_app.application import orders
from fixture_app.application.ports import good
from fixture_app.bootstrap import main
from fixture_app.interfaces import web


@dataclass
class Record:
    order_id: str


class RecordSource(Protocol):
    def fetch(self) -> Record: ...


class OrderService:
    def run(self) -> str:
        return orders.NAME + impl.VALUE + web.NAME + main.NAME + good.__name__


class Holder:
    def __init__(self, source: RecordSource) -> None:
        self.source = source

    async def load(self) -> Record:
        return self.source.fetch()
