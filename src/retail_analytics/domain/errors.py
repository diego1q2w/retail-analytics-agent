"""Errors raised by domain rules."""

from __future__ import annotations


class InvalidTransition(Exception):
    """A status change the lifecycle does not allow."""

    def __init__(self, record: str, current: str, requested: str) -> None:
        self.record = record
        self.current = current
        self.requested = requested
        super().__init__(f"{record} cannot move from {current} to {requested}")
