"""Ports module that is not only ports (each flagged item is deliberate)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fixture_app.application import orders
from fixture_app.adapters import impl

DEFAULT_LIMIT = 10


@dataclass
class OrderRecord:
    order_id: str


class ConcreteStore:
    def fetch(self) -> str:
        return "x"


class LogicPort(Protocol):
    def fetch(self) -> str:
        return orders.NAME


def helper() -> int:
    return impl.VALUE
