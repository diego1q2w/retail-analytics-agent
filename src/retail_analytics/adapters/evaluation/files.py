"""Read manifests and results, write results atomically, replay recordings."""

from __future__ import annotations

import json
import os
from pathlib import Path

from retail_analytics.application.evaluation.manifest import Manifest
from retail_analytics.application.evaluation.ports import (
    ScenarioInput,
    TargetObservation,
    TargetUnavailable,
)
from retail_analytics.application.evaluation.results import RunResult, serialize_result


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def load_result(path: Path) -> RunResult:
    return RunResult.model_validate_json(path.read_text(encoding="utf-8"))


def write_result(path: Path, result: RunResult) -> None:
    """Write via a temp file so a reader never sees a partial result."""
    text = serialize_result(result)  # refuses sensitive-looking content first
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class ReplayTarget:
    """Replays recorded observations ``{scenario_id: observation}`` from a file.

    Gives a deterministic fixture-mode target that needs no agent, model or
    network. A scenario without a recording is blocked, not passed.
    """

    def __init__(self, observations_path: Path) -> None:
        raw = json.loads(observations_path.read_text(encoding="utf-8"))
        self.target_id = f"replay:{observations_path.name}"
        self._observations = {
            sid: TargetObservation.model_validate(obs) for sid, obs in raw.items()
        }

    def run(self, case: ScenarioInput) -> TargetObservation:
        try:
            return self._observations[case.scenario_id]
        except KeyError:
            raise TargetUnavailable from None
