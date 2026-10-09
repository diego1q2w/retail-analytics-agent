"""Golden retrieval benchmark: ``python -m retail_analytics.bootstrap.retrieval_eval``.

Commands (run from the repository root with a migrated database; semantic
variants need ``EMBEDDING_PROVIDER=gemini`` and a key):

- ``prepare``: create two synthetic evaluation executives and publish the
  corpus (seeds plus evaluation-only examples). Safe to rerun; embeddings are
  computed once per (content digest, model) and kept in PostgreSQL.
- ``manifest``: regenerate ``evaluation/retrieval/manifest.json`` from the labels.
- ``sweep``: threshold sweep on the tuning split only.
- ``benchmark``: run every variant on one or both splits through the evaluation
  runner, write result files and print the comparison table.

Use a throwaway database: ``prepare`` publishes synthetic examples.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass
from pathlib import Path

import click

from retail_analytics.adapters.embedding.hashing import HashingEmbedder
from retail_analytics.adapters.embedding.query_cache import CachedQueryEmbedder
from retail_analytics.adapters.evaluation.files import load_result, write_result
from retail_analytics.adapters.evaluation.retrieval_files import (
    DATA_DIR,
    load_corpus,
    load_labels,
    write_manifest,
)
from retail_analytics.application.authorization import (
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.contracts.authorization import ExecutiveRegistration
from retail_analytics.application.evaluation.manifest import Manifest
from retail_analytics.application.evaluation.retrieval_labels import (
    EvalCorpus,
    LabelSet,
    validate_labels,
)
from retail_analytics.application.evaluation.retrieval_report import (
    VariantSummary,
    render,
    render_categories,
    summarize,
)
from retail_analytics.application.evaluation.retrieval_target import (
    RetrievalEvalTarget,
    Retrieve,
    build_manifest,
    corpus_entries,
    load_eval_corpus,
)
from retail_analytics.application.evaluation.retrieval_tuning import best, sweep
from retail_analytics.application.evaluation.runner import RunConfig, run_manifest
from retail_analytics.application.golden_seed_library import (
    SEED_SCHEMA_VERSION,
    seed_library,
)
from retail_analytics.application.golden_seeding import seed_principals
from retail_analytics.application.ports.retrieval import TextEmbedder
from retail_analytics.application.retrieval import (
    GoldenIndex,
    GoldenRetriever,
    RetrievalUnavailable,
)
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    load_backend_settings,
)
from retail_analytics.bootstrap.knowledge import KnowledgeServices, build_knowledge
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.bootstrap.retrieval import build_embedder
from retail_analytics.domain.access import Role
from retail_analytics.domain.knowledge import ApplicabilityContext
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.retrieval import (
    CHANNELS,
    LEXICAL,
    PLACEHOLDER_MIN_LEXICAL_COVERAGE,
    PLACEHOLDER_MIN_SIMILARITY,
    SEMANTIC,
    RetrievalConfig,
)

AUTHOR_ID = "eval-author"
REVIEWER_ID = "eval-reviewer"
OUT_DIR = Path("evaluation-results") / "retrieval"
OPEN = {"min_similarity": -1.0, "min_lexical_coverage": 0.0}

# Variant name -> channels and thresholds. "open" variants keep every candidate
# (up to the three-result cap) to compare ranking; "gated" variants apply the
# thresholds, so they also show no-match behavior. The tuned thresholds below
# come from ``sweep`` on the tuning split (see evaluation/retrieval/README.md).
TUNED_SIMILARITY = 0.70
TUNED_LEXICAL_COVERAGE = 0.75
# Keyword-only's best coverage on tuning (0.5) equals the placeholder, so it has
# no separate tuned variant. Semantic-only's best similarity from its own sweep:
TUNED_SEMANTIC_SIMILARITY = 0.70


@dataclass(frozen=True)
class Variant:
    name: str
    channels: frozenset[str]
    similarity: float
    coverage: float
    weight: float = 1.0

    @property
    def config(self) -> RetrievalConfig:
        return RetrievalConfig(
            channels=self.channels,
            min_similarity=self.similarity,
            min_lexical_coverage=self.coverage,
            semantic_weight=self.weight,
        )


def variants() -> dict[str, Variant]:
    defaults = RetrievalConfig()
    placeholder = (PLACEHOLDER_MIN_SIMILARITY, PLACEHOLDER_MIN_LEXICAL_COVERAGE)
    table = [
        Variant("keyword-open", frozenset({LEXICAL}), -1.0, 0.0),
        Variant("semantic-open", frozenset({SEMANTIC}), -1.0, 0.0),
        Variant("fused-open", CHANNELS, -1.0, 0.0),
        Variant(
            "keyword-placeholder",
            frozenset({LEXICAL}),
            *placeholder,
        ),
        Variant(
            "semantic-placeholder",
            frozenset({SEMANTIC}),
            *placeholder,
        ),
        Variant(
            "fused-placeholder",
            CHANNELS,
            *placeholder,
        ),
        Variant(
            "semantic-tuned",
            frozenset({SEMANTIC}),
            TUNED_SEMANTIC_SIMILARITY,
            0.0,
        ),
        Variant("fused-tuned", CHANNELS, TUNED_SIMILARITY, TUNED_LEXICAL_COVERAGE),
        # T36-F1: weighted fusion (semantic primary); "default" is the shipped config.
        Variant("fused-weighted-open", CHANNELS, -1.0, 0.0, defaults.semantic_weight),
        Variant(
            "fused-default",
            CHANNELS,
            defaults.min_similarity,
            defaults.min_lexical_coverage,
            defaults.semantic_weight,
        ),
    ]
    return {v.name: v for v in table}


@dataclass
class Session:
    settings: BackendSettings
    persistence: Persistence
    knowledge: KnowledgeServices
    resolver: AccessResolver
    labels: LabelSet
    corpus: EvalCorpus

    def close(self) -> None:
        self.persistence.close()


def open_session() -> Session:
    try:
        settings = load_backend_settings()
        persistence = persistence_from_settings(settings)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    guard = OwnershipGuard(
        persistence.sessions, persistence.runs, persistence.tool_executions
    )
    resolver = AccessResolver(persistence.executives, guard)
    knowledge = build_knowledge(
        persistence, build_artifacts(settings, persistence), resolver
    )
    labels = load_labels(DATA_DIR / "labels.json")
    corpus = load_corpus(DATA_DIR / "corpus.json")
    problems = validate_labels(
        labels,
        frozenset(corpus_entries(corpus)),
        {e.key: e.question for e in corpus.examples},
        [s.question for s in seed_library()],
    )
    if problems:
        persistence.close()
        raise click.ClickException("labels are not sound: " + "; ".join(problems))
    return Session(settings, persistence, knowledge, resolver, labels, corpus)


def all_products(labels: LabelSet) -> frozenset[str]:
    return frozenset(itertools.chain.from_iterable(labels.scopes.values()))


def metric_versions() -> dict[str, int]:
    catalog = default_catalog()
    return {m: catalog.latest_version(m) for m in sorted(catalog.metric_ids())}


def embedder_for(session: Session, channels: frozenset[str]) -> TextEmbedder:
    """Keyword-only variants never need a provider; others use the configured one."""
    if channels == frozenset({LEXICAL}):
        return HashingEmbedder()
    inner = build_embedder(session.settings)
    return CachedQueryEmbedder(inner, OUT_DIR / "query-embeddings.json")


def make_retriever(
    session: Session, embedder: TextEmbedder, config: RetrievalConfig
) -> GoldenRetriever:
    index = GoldenIndex(
        session.knowledge.index_source, embedder, session.knowledge.embeddings
    )
    return GoldenRetriever(index, session.knowledge.reader, config)


@click.group()
def main() -> None:
    """Golden retrieval benchmark."""


@main.command()
def prepare() -> None:
    """Provision the evaluation executives and publish the corpus."""
    session = open_session()
    try:
        asyncio.run(_prepare(session))
    finally:
        session.close()
    click.echo(
        f"corpus ready: {len(seed_library())} seeds + "
        f"{len(session.corpus.examples)} evaluation examples"
    )


async def _prepare(session: Session) -> None:
    admin = session.persistence.access_admin
    products = all_products(session.labels)
    for executive_id, role in (
        (AUTHOR_ID, Role.EXECUTIVE),
        (REVIEWER_ID, Role.REVIEWER),
    ):
        await admin.register_executive(
            ExecutiveRegistration(
                executive_id=executive_id,
                issuer=session.settings.auth_issuer,
                subject=executive_id,
                roles=frozenset({role}),
                label="Synthetic retrieval-evaluation executive",
            )
        )
        await admin.replace_products(executive_id, products)
    author, reviewer = seed_principals(AUTHOR_ID, REVIEWER_ID)
    await load_eval_corpus(session.knowledge.service, author, reviewer, session.corpus)


@main.command()
@click.option(
    "--out", type=click.Path(path_type=Path), default=DATA_DIR / "manifest.json"
)
def manifest(out: Path) -> None:
    """Regenerate the benchmark manifest from the labels."""
    labels = load_labels(DATA_DIR / "labels.json")
    write_manifest(out, build_manifest(labels))
    click.echo(f"manifest written to {out}")


GRID_SIMILARITY = (-1.0, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75)
GRID_COVERAGE = (0.0, 0.25, 0.34, 0.5, 0.67, 0.75, 1.0)
GRID_WEIGHT = (1.0, 2.0, 3.0, 5.0, 10.0)


@main.command(name="sweep")
@click.option(
    "--channels",
    type=click.Choice(["lexical", "semantic", "fused"]),
    default="fused",
    show_default=True,
)
def sweep_command(channels: str) -> None:
    """Sweep thresholds on the tuning split and print the ranked settings."""
    session = open_session()
    try:
        selected = CHANNELS if channels == "fused" else frozenset({channels})
        try:
            embedder = embedder_for(session, selected)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from None
        similarities = GRID_SIMILARITY if SEMANTIC in selected else (-1.0,)
        coverages = GRID_COVERAGE if LEXICAL in selected else (0.0,)
        weights = GRID_WEIGHT if selected == CHANNELS else (1.0,)
        configs = [
            RetrievalConfig(
                channels=selected,
                min_similarity=s,
                min_lexical_coverage=c,
                semantic_weight=w,
            )
            for s in similarities
            for c in coverages
            for w in weights
        ]
        entries = corpus_entries(session.corpus)
        by_example = {e.example_id: k for k, e in entries.items()}
        context = ApplicabilityContext(SEED_SCHEMA_VERSION, metric_versions())
        scopes = {k: frozenset(v) for k, v in session.labels.scopes.items()}
        retrievers: dict[RetrievalConfig, GoldenRetriever] = {}

        def make(config: RetrievalConfig) -> Retrieve:
            if config not in retrievers:
                retrievers[config] = make_retriever(session, embedder, config)
            return retrievers[config].retrieve

        try:
            rows = asyncio.run(
                sweep(
                    make,
                    configs,
                    session.labels.split("tuning"),
                    scopes,
                    entries,
                    by_example,
                    context,
                )
            )
        except RetrievalUnavailable as exc:
            raise click.ClickException(
                f"embeddings unavailable: {type(exc.__cause__ or exc).__name__}"
            ) from None
    finally:
        session.close()
    click.echo(
        "min_sim min_cov weight recall@3 precision@3 mrr no_match "
        "declines exposures obj"
    )
    for row in sorted(rows, key=lambda r: -r.objective):
        c = row.config
        click.echo(
            f"{c.min_similarity:7.2f} {c.min_lexical_coverage:7.2f} "
            f"{c.semantic_weight:6.1f} "
            f"{row.recall_at_3:8.3f} {row.precision_at_3:11.3f} {row.mrr:5.3f} "
            f"{row.no_match_rate:8.3f} {row.false_declines:8d} "
            f"{row.exposures:9d} {row.objective:5.3f}"
        )
    top = best(rows).config
    click.echo(
        f"best on tuning ({channels}): min_similarity={top.min_similarity} "
        f"min_lexical_coverage={top.min_lexical_coverage} "
        f"semantic_weight={top.semantic_weight}"
    )


@main.command()
@click.option("--variant", "names", multiple=True, help="Default: every variant.")
@click.option(
    "--split",
    "splits",
    multiple=True,
    type=click.Choice(["tuning", "heldout"]),
    help="Default: both.",
)
def benchmark(names: tuple[str, ...], splits: tuple[str, ...]) -> None:
    """Run variants through the evaluation runner and print the comparison."""
    session = open_session()
    table = variants()
    chosen = names or tuple(table)
    summaries = []
    try:
        entries = corpus_entries(session.corpus)
        manifest_ = build_manifest(session.labels)
        for name in chosen:
            variant = table[name]
            try:
                embedder = embedder_for(session, variant.channels)
            except ConfigError:
                click.echo(f"{name}: blocked (embedding provider not configured)")
                continue
            retriever = make_retriever(session, embedder, variant.config)
            target = RetrievalEvalTarget(
                f"retrieval:{name}",
                retriever.retrieve,
                session.labels,
                entries,
                metric_versions(),
            )
            try:
                summaries.extend(
                    _run_variant(session, manifest_, target, variant, splits)
                )
            finally:
                target.close()
            OUT_DIR.joinpath("traces").mkdir(parents=True, exist_ok=True)
            OUT_DIR.joinpath("traces", f"{name}.json").write_text(
                json.dumps(target.trace, indent=1, sort_keys=True), encoding="utf-8"
            )
    finally:
        session.close()
    click.echo(render(summaries))


def _run_variant(
    session: Session,
    manifest_: Manifest,
    target: RetrievalEvalTarget,
    variant: Variant,
    splits: tuple[str, ...],
) -> list[VariantSummary]:
    out: list[VariantSummary] = []
    for split in splits or ("tuning", "heldout"):
        config = RunConfig(
            mode="live",
            available_capabilities=frozenset({"database"}),
            versions={
                "retrieval": variant.config.version,
                "corpus": f"{session.corpus.corpus_version}+seed-{SEED_SCHEMA_VERSION}",
                "dataset": session.labels.labels_version,
            },
            tags=frozenset({split}),
        )
        result = run_manifest(manifest_, target, config)
        write_result(OUT_DIR / f"{variant.name}-{split}.json", result)
        out.append(summarize(variant.name, result, session.labels, split))
    return out


@main.command()
@click.option("--split", type=click.Choice(["tuning", "heldout"]), default="heldout")
@click.option(
    "--by-category", "category_variants", multiple=True, help="Variant to break down."
)
def report(split: str, category_variants: tuple[str, ...]) -> None:
    """Rebuild the comparison table from stored result files."""
    labels = load_labels(DATA_DIR / "labels.json")
    summaries = []
    for name in variants():
        path = OUT_DIR / f"{name}-{split}.json"
        if path.exists():
            summaries.append(summarize(name, load_result(path), labels, split))
    click.echo(render(summaries))
    for name in category_variants:
        click.echo(
            render_categories(
                name, load_result(OUT_DIR / f"{name}-{split}.json"), labels, split
            )
        )


if __name__ == "__main__":
    main()
