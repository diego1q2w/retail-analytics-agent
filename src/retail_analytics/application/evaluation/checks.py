"""Deterministic expectation checks. Pure functions, no I/O, no model calls."""

from __future__ import annotations

import math

from retail_analytics.application.evaluation.manifest import (
    ExactExpectation,
    Expectation,
    NumericExpectation,
    Scalar,
    TextExpectation,
    ToolExpectation,
)
from retail_analytics.application.evaluation.ports import TargetObservation
from retail_analytics.application.evaluation.results import (
    CheckDetail,
    CheckResult,
    sha256_hex,
)


def tolerance(expected: float, abs_tol: float, rel_tol: float) -> float:
    return max(abs_tol, rel_tol * abs(expected))


def _numeric(exp: NumericExpectation, obs: TargetObservation) -> CheckResult:
    def result(
        passed: bool, detail: CheckDetail, observed: float | None = None
    ) -> CheckResult:
        return CheckResult(
            name=exp.name,
            kind="numeric",
            passed=passed,
            detail=detail,
            expected=exp.expected,
            observed=observed,
        )

    if exp.name not in obs.values:
        return result(False, "missing_observation")
    value = obs.values[exp.name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return result(False, "wrong_type")
    observed = float(value)
    if not math.isfinite(observed):
        return result(False, "non_finite")
    ok = abs(observed - exp.expected) <= tolerance(
        exp.expected, exp.abs_tol, exp.rel_tol
    )
    return result(ok, "ok" if ok else "out_of_tolerance", observed)


def _exact(exp: ExactExpectation, obs: TargetObservation) -> CheckResult:
    if exp.name not in obs.values:
        return CheckResult(
            name=exp.name, kind="exact", passed=False, detail="missing_observation"
        )
    value = obs.values[exp.name]
    # Same type and value: 1 must not satisfy True or 1.0.
    ok = type(value) is type(exp.expected) and value == exp.expected

    def number(item: Scalar) -> float | bool | None:
        return item if isinstance(item, bool | int | float) else None

    def digest(item: Scalar) -> str | None:
        return sha256_hex(item) if isinstance(item, str) else None

    return CheckResult(
        name=exp.name,
        kind="exact",
        passed=ok,
        detail="ok" if ok else "mismatch",
        expected=number(exp.expected),
        observed=number(value),
        expected_digest=digest(exp.expected),
        observed_digest=digest(value),
    )


def _text(exp: TextExpectation, obs: TargetObservation) -> CheckResult:
    found = exp.needle in obs.answer_text
    ok = found if exp.kind == "contains" else not found
    return CheckResult(
        name=exp.name,
        kind=exp.kind,
        passed=ok,
        detail="found" if found else "not_found",
        expected_digest=sha256_hex(exp.needle),
    )


def _tool(exp: ToolExpectation, obs: TargetObservation) -> CheckResult:
    called = exp.tool in obs.tool_calls
    ok = called if exp.kind == "tool_called" else not called
    return CheckResult(
        name=exp.name,
        kind=exp.kind,
        passed=ok,
        detail="found" if called else "not_found",
    )


def evaluate_expectation(exp: Expectation, obs: TargetObservation) -> CheckResult:
    if isinstance(exp, NumericExpectation):
        return _numeric(exp, obs)
    if isinstance(exp, ExactExpectation):
        return _exact(exp, obs)
    if isinstance(exp, TextExpectation):
        return _text(exp, obs)
    return _tool(exp, obs)
