"""The ``list_relations`` and ``describe_relation`` capabilities.

Both render the same authorized ``CatalogView`` the SQL compiler validates
against, so a field or type shown here is exactly what a query may use.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from pydantic import StringConstraints

from retail_analytics.application.contracts import ContractModel
from retail_analytics.application.discovery import (
    CatalogUnavailable,
    DiscoveryService,
    DiscoveryView,
    RelationNotAvailable,
)
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilitySpec,
    OperationContext,
    RetrySpec,
    ToolFailed,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSucceeded,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.catalog import RelationView
from retail_analytics.domain.operations import (
    RecoveryMode,
    SideEffect,
    ToolErrorCode,
)

LIST_RELATIONS = "list_relations"
DESCRIBE_RELATION = "describe_relation"

_UNAVAILABLE_MESSAGE = "That relation is not available. Use list_relations."
_METADATA_MESSAGE = "Schema information is temporarily unavailable. Retry shortly."


class RelationSummary(ContractModel):
    name: str
    description: str
    grain: str


class FieldDescription(ContractModel):
    name: str
    type: str
    description: str


class JoinDescription(ContractModel):
    field: str
    target_relation: str
    target_field: str
    cardinality: str


class Freshness(ContractModel):
    catalog_version: int
    entitlement_version: int
    metadata_as_of: datetime
    # True when the source metadata could not be refreshed on schedule.
    metadata_stale: bool


class ListRelationsInput(ToolInput):
    pass


class ListRelationsOutput(ToolOutput):
    relations: tuple[RelationSummary, ...]
    freshness: Freshness


class DescribeRelationInput(ToolInput):
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class DescribeRelationOutput(ToolOutput):
    name: str
    description: str
    grain: str
    fields: tuple[FieldDescription, ...]
    joins: tuple[JoinDescription, ...]
    # Reviewed fields that are temporarily disabled; logical names only.
    unavailable_fields: tuple[str, ...]
    freshness: Freshness


def _freshness(view: DiscoveryView) -> Freshness:
    return Freshness(
        catalog_version=view.catalog.catalog_version,
        entitlement_version=view.catalog.entitlement_version,
        metadata_as_of=view.metadata_as_of,
        metadata_stale=view.stale,
    )


def _description(relation: RelationView, view: DiscoveryView) -> DescribeRelationOutput:
    return DescribeRelationOutput(
        name=relation.name,
        description=relation.description,
        grain=relation.grain,
        fields=tuple(
            FieldDescription(name=f.name, type=f.type.value, description=f.description)
            for f in relation.fields
        ),
        joins=tuple(
            JoinDescription(
                field=j.field,
                target_relation=j.target,
                target_field=j.target_field,
                cardinality=j.cardinality.value,
            )
            for j in relation.joins
        ),
        unavailable_fields=relation.unavailable_fields,
        freshness=_freshness(view),
    )


def _authorization() -> AuthorizationSpec:
    return AuthorizationSpec(
        required_permissions=frozenset({Permission.ANALYSIS_READ.value}),
        requires_product_scope=True,
    )


def discovery_capabilities(
    service: DiscoveryService,
) -> tuple[
    CapabilitySpec[ListRelationsInput, ListRelationsOutput],
    CapabilitySpec[DescribeRelationInput, DescribeRelationOutput],
]:
    async def list_relations(
        _: ListRelationsInput, ctx: OperationContext
    ) -> ToolOutcome[ListRelationsOutput]:
        try:
            view = await service.list_relations(ctx.execution)
        except CatalogUnavailable:
            return ToolFailed(
                code=ToolErrorCode.TEMPORARY_FAILURE, message=_METADATA_MESSAGE
            )
        relations = tuple(
            RelationSummary(name=r.name, description=r.description, grain=r.grain)
            for r in view.catalog.relations.values()
        )
        return ToolSucceeded(
            output=ListRelationsOutput(relations=relations, freshness=_freshness(view)),
            empty=not relations,
        )

    async def describe_relation(
        args: DescribeRelationInput, ctx: OperationContext
    ) -> ToolOutcome[DescribeRelationOutput]:
        try:
            view, relation = await service.describe_relation(ctx.execution, args.name)
        except RelationNotAvailable:
            return ToolFailed(
                code=ToolErrorCode.FIELD_UNAVAILABLE, message=_UNAVAILABLE_MESSAGE
            )
        except CatalogUnavailable:
            return ToolFailed(
                code=ToolErrorCode.TEMPORARY_FAILURE, message=_METADATA_MESSAGE
            )
        return ToolSucceeded(output=_description(relation, view))

    retry = _read_retry()
    return (
        CapabilitySpec(
            name=LIST_RELATIONS,
            version=1,
            description=(
                "List the analytical relations you may query, with their grain."
            ),
            progress_label="Checking which data is available.",
            input_model=ListRelationsInput,
            output_model=ListRelationsOutput,
            handler=list_relations,
            authorization=_authorization(),
            side_effect=SideEffect.READ_ONLY,
            retry=retry,
        ),
        CapabilitySpec(
            name=DESCRIBE_RELATION,
            version=1,
            description=(
                "Describe one relation: its fields and types, row grain and the "
                "joins you may use. Only these fields can appear in queries."
            ),
            progress_label="Reading the data definition.",
            input_model=DescribeRelationInput,
            output_model=DescribeRelationOutput,
            handler=describe_relation,
            authorization=_authorization(),
            side_effect=SideEffect.READ_ONLY,
            retry=retry,
        ),
    )


def _read_retry() -> RetrySpec:
    return RetrySpec(RecoveryMode.RETRY, 2, timedelta(seconds=30))
