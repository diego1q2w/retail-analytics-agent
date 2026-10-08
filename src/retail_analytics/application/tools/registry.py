"""Reviewed capability registry and the permission-filtered tool catalog.

Adding a capability means adding one ``CapabilitySpec`` at the composition
root; the gateway and agent loop do not change. Specs are validated when they
are created so an unsafe one (trusted fields in its arguments, permissive
input models, blind retries of external effects) cannot be registered.

The catalog depends only on the trusted context, never on an investigation
stage, so the model sees the same small tool set on every iteration.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import Field, JsonValue

from retail_analytics.application.contracts import CapabilityName, ContractModel
from retail_analytics.application.tools.context import (
    ExecutionContext,
    OperationContext,
)
from retail_analytics.application.tools.contracts import (
    ToolInput,
    ToolOutcome,
    ToolOutput,
)
from retail_analytics.domain.operations import RecoveryMode, SideEffect

MAX_LABEL_LENGTH = 280
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Argument names that would let the model claim identity, authority, budgets or
# approval. Matched as substrings of every property name in the input schema,
# including nested models. Trusted values come from ExecutionContext instead.
RESERVED_ARGUMENT_TOKENS: frozenset[str] = frozenset(
    {
        "executive",
        "identity",
        "entitlement",
        "permission",
        "authoriz",
        "budget",
        "confirm",
        "approv",
        "credential",
    }
)
RESERVED_ARGUMENT_NAMES: frozenset[str] = frozenset(
    {
        "user_id",
        "session_id",
        "run_id",
        "trace_id",
        "operation_id",
        "product_scope",
        "permitted_products",
        "allowed_products",
    }
)


class RegistrationError(ValueError):
    """A capability spec violates the registry's safety rules."""


@dataclass(frozen=True, slots=True)
class AuthorizationSpec:
    """Who may see and run a capability; checked again on every execution."""

    required_permissions: frozenset[str] = frozenset()
    # Hide the capability when the executive has no permitted products.
    requires_product_scope: bool = False

    def allows(self, context: ExecutionContext) -> bool:
        if not self.required_permissions <= context.permissions:
            return False
        return not (self.requires_product_scope and context.product_scope.is_empty)


@dataclass(frozen=True, slots=True)
class RetrySpec:
    """This capability's recovery behaviour; the run budget may cap it further."""

    recovery: RecoveryMode
    max_attempts: int
    attempt_timeout: timedelta

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise RegistrationError("max_attempts must be at least 1")
        if self.recovery is RecoveryMode.NO_RETRY and self.max_attempts != 1:
            raise RegistrationError("NO_RETRY allows exactly one attempt")
        if self.attempt_timeout <= timedelta(0):
            raise RegistrationError("attempt_timeout must be positive")


type Handler[InputT: ToolInput, OutputT: ToolOutput] = Callable[
    [InputT, OperationContext], Awaitable[ToolOutcome[OutputT]]
]


@dataclass(frozen=True, slots=True)
class CapabilitySpec[InputT: ToolInput, OutputT: ToolOutput]:
    name: str
    version: int
    description: str
    # Application-authored text for the tool.started progress event.
    progress_label: str
    input_model: type[InputT]
    output_model: type[OutputT]
    handler: Handler[InputT, OutputT]
    authorization: AuthorizationSpec
    side_effect: SideEffect
    retry: RetrySpec

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise RegistrationError(f"invalid capability name {self.name!r}")
        if self.version < 1:
            raise RegistrationError(f"{self.name}: version must be >= 1")
        if not self.description.strip():
            raise RegistrationError(f"{self.name}: description required")
        if not 0 < len(self.progress_label.strip()) <= MAX_LABEL_LENGTH:
            raise RegistrationError(f"{self.name}: progress_label must be 1-280 chars")
        _check_model(self.name, self.input_model, ToolInput)
        _check_model(self.name, self.output_model, ToolOutput)
        _check_arguments(self.name, self.input_model)
        _check_recovery(self.name, self.side_effect, self.retry.recovery)


def _check_model(name: str, model: type[Any], base: type[ContractModel]) -> None:
    if not (isinstance(model, type) and issubclass(model, base)):
        raise RegistrationError(f"{name}: {model!r} must subclass {base.__name__}")
    if model.model_config.get("extra") != "forbid":
        raise RegistrationError(f"{name}: {model.__name__} must forbid extra fields")


def _schema_property_names(schema: JsonValue) -> Iterator[str]:
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            yield from properties
        for value in schema.values():
            yield from _schema_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _schema_property_names(item)


def _check_arguments(name: str, model: type[ToolInput]) -> None:
    for prop in _schema_property_names(model.model_json_schema()):
        lowered = prop.lower()
        if lowered in RESERVED_ARGUMENT_NAMES or any(
            token in lowered for token in RESERVED_ARGUMENT_TOKENS
        ):
            raise RegistrationError(
                f"{name}: argument {prop!r} is trusted context, not model input"
            )


def _check_recovery(name: str, effect: SideEffect, recovery: RecoveryMode) -> None:
    if effect.may_leave_external_effect and recovery is RecoveryMode.RETRY:
        raise RegistrationError(
            f"{name}: {effect} must reconcile before retry or not retry at all"
        )


class ToolDescriptor(ContractModel):
    """Model-facing description of one catalog tool."""

    name: CapabilityName
    version: int = Field(ge=1)
    description: str
    parameters: dict[str, JsonValue]


class CapabilityRegistry:
    """Immutable set of reviewed capabilities, one version per name."""

    def __init__(self, specs: Iterable[CapabilitySpec[Any, Any]]) -> None:
        by_name: dict[str, CapabilitySpec[Any, Any]] = {}
        for spec in specs:
            if spec.name in by_name:
                raise RegistrationError(f"duplicate capability {spec.name!r}")
            by_name[spec.name] = spec
        self._by_name = by_name
        self._descriptors = {
            name: ToolDescriptor(
                name=name,
                version=spec.version,
                description=spec.description,
                parameters=spec.input_model.model_json_schema(),
            )
            for name, spec in sorted(by_name.items())
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._descriptors)

    def catalog(self, context: ExecutionContext) -> tuple[ToolDescriptor, ...]:
        """Tools this executive may use now, in a stable order."""
        return tuple(
            descriptor
            for name, descriptor in self._descriptors.items()
            if self._by_name[name].authorization.allows(context)
        )

    def resolve(
        self, name: CapabilityName, context: ExecutionContext
    ) -> CapabilitySpec[Any, Any] | None:
        """The spec if it exists and is authorized for ``context``, else None.

        Unknown and unauthorized names are indistinguishable to callers so the
        model cannot probe for hidden capabilities.
        """
        spec = self._by_name.get(name)
        if spec is None or not spec.authorization.allows(context):
            return None
        return spec
