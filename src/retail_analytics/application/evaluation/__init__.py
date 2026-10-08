"""Repeatable evaluation: scenario manifest, runner, result format, comparison.

The runner knows nothing about the agent. It drives an ``EvaluationTarget``
(any object implementing the port) with the dialogue of each scenario and checks
what comes back. Deterministic checks, judge scores and operational measurements
are kept in separate sections of every result.
"""
