"""Read the labeled retrieval data and write the benchmark manifest."""

from __future__ import annotations

import json
import os
from pathlib import Path

from retail_analytics.application.evaluation.manifest import Manifest
from retail_analytics.application.evaluation.retrieval_labels import (
    EvalCorpus,
    LabelSet,
)

# Relative to the working directory unless the variable names another folder
# (for example a checkout while running from a directory with credentials).
DATA_DIR_ENV = "RETRIEVAL_EVAL_DIR"
DATA_DIR = Path(os.environ.get(DATA_DIR_ENV) or Path("evaluation") / "retrieval")


def load_labels(path: Path) -> LabelSet:
    return LabelSet.model_validate_json(path.read_text(encoding="utf-8"))


def load_corpus(path: Path) -> EvalCorpus:
    return EvalCorpus.model_validate_json(path.read_text(encoding="utf-8"))


def write_manifest(path: Path, manifest: Manifest) -> None:
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=1) + "\n",
        encoding="utf-8",
    )
