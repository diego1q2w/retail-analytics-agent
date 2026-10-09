"""Adapter that takes a port from a service module instead of application.ports."""

from fixture_app.application.orders import GoodPort

VALUE = "x"


class Impl:
    port = GoodPort
