"""The shared agent and general composition load and run with Temporal blocked.

A fresh interpreter refuses every import of ``temporalio``, Pydantic AI's
Temporal integration and the project's Temporal adapter (Pydantic AI's core
itself imports the engine-neutral ``pydantic_ai.durable_exec`` package). It
then imports the shared agent, the lifecycle policy and the general
investigation composition root, and runs answer, clarification and
context-change investigations against controlled services. Nothing needs
uninstalling and no service is contacted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BLOCKED = (
    "temporalio",
    "pydantic_ai.durable_exec.temporal",
    "retail_analytics.adapters.temporal",
)

_PROBE = r"""
import asyncio, importlib.abc, json, sys
from dataclasses import replace

repo, src, blocked = sys.argv[1], sys.argv[2], tuple(json.loads(sys.argv[3]))
sys.path[:0] = [src, repo]
refused = []

class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in blocked):
            refused.append(name)
            raise ImportError("blocked: " + name)
        return None

sys.meta_path.insert(0, Blocked())

import retail_analytics.bootstrap.investigations  # general composition root
from retail_analytics.adapters.agent.investigator import proposal, run_investigation
from retail_analytics.application import investigation_lifecycle as lifecycle
from retail_analytics.application.contracts.investigations import (
    AgentInterruption, InterruptionKind, StepOutcome, StepResult,
)
from retail_analytics.application.investigation_runtime import (
    InvestigationContextChanged,
)
from tests.unit.agent.scenario import STEP, scenario

async def main():
    out = {}
    answered = proposal("r", 0, await run_investigation(scenario().agent, "r"))
    out["answer"] = [type(answered).__name__, answered.text]
    asked = proposal(
        "r", 1, await run_investigation(scenario(ends_with="clarify").agent, "r")
    )
    out["clarify"] = [
        type(asked).__name__,
        lifecycle.after_output(StepOutcome(StepResult.ASKED)).action.value,
    ]
    stale = scenario(STEP, replace(STEP, history_key="scope-v2"))
    try:
        await run_investigation(stale.agent, "r")
        out["context"] = "not detected"
    except InvestigationContextChanged:
        decision = lifecycle.after_interruption(
            AgentInterruption(InterruptionKind.CONTEXT_CHANGED)
        )
        out["context"] = [len(stale.provider.requests), decision.action.value]
    return out

result = asyncio.run(main())
result["loaded"] = sorted(
    m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in blocked)
)
result["refused"] = sorted(set(refused))
print(json.dumps(result))
"""


def test_shared_agent_runs_with_temporal_imports_blocked() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _PROBE,
            str(REPO),
            str(REPO / "src"),
            json.dumps(BLOCKED),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={"PYDANTIC_AI_NO_BANNER": "1"},
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    # Nothing loaded them, and nothing even tried (and silently fell back).
    assert report["loaded"] == [] and report["refused"] == []
    assert report["answer"] == ["AnswerDraft", "Sales grew 4%."]
    assert report["clarify"] == ["QuestionDraft", "await_input"]
    assert report["context"] == [1, "investigate"]


def test_probe_blocks_the_temporal_adapter() -> None:
    """The guard is real: the Temporal adapter cannot load under it."""
    probe = (
        "import importlib.abc, sys\n"
        "sys.path[:0] = [sys.argv[1]]\n"
        "class B(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'temporalio' or name.startswith('temporalio.'):\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, B())\n"
        "import retail_analytics.adapters.temporal.agent\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe, str(REPO / "src")],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={"PYDANTIC_AI_NO_BANNER": "1"},
    )
    assert completed.returncode != 0
    assert "ImportError" in completed.stderr


_LOCAL_PROBE = r"""
import asyncio, importlib.abc, json, sys

repo, src, blocked = sys.argv[1], sys.argv[2], tuple(json.loads(sys.argv[3]))
sys.path[:0] = [src, repo]
refused = []

class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in blocked):
            refused.append(name)
            raise ImportError("blocked: " + name)
        return None

sys.meta_path.insert(0, Blocked())

from pydantic_ai.models.function import FunctionModel
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.local_investigations import (
    build_local_investigations,
)
from retail_analytics.bootstrap.persistence import build_persistence
from tests.unit.local.fakes import harness

async def main():
    out = {}
    # Full local composition (an engine connects lazily: nothing is contacted).
    persistence = build_persistence("postgresql+psycopg://u:p@127.0.0.1:9/none")
    local = build_local_investigations(
        BackendSettings(mode="fixture"),
        persistence,
        build_access(persistence, verifier=None),
        FunctionModel(lambda messages, info: None),
    )
    out["backend"] = local.manager.backend.value
    persistence.close()
    # Local execution: shared agent + lifecycle decisions in-process.
    h = harness(ends_with="clarify")
    await h.manager.open()
    await h.manager.start("r1")
    while h.runtime.steps("r1")[-1:] != ["resume"]:
        await asyncio.sleep(0.01)
    h.runtime.add_input("r1")
    await h.manager.notify_input("r1")
    while h.manager.running():
        await asyncio.sleep(0.01)
    await h.manager.close()
    out["steps"] = h.runtime.steps("r1")
    out["answers"] = [a.text for a in h.runtime.answers]
    return out

result = asyncio.run(main())
result["loaded"] = sorted(
    m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in blocked)
)
result["refused"] = sorted(set(refused))
print(json.dumps(result))
"""


def test_local_manager_builds_and_runs_with_temporal_imports_blocked() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _LOCAL_PROBE,
            str(REPO),
            str(REPO / "src"),
            json.dumps(BLOCKED),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={"PYDANTIC_AI_NO_BANNER": "1"},
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["loaded"] == [] and report["refused"] == []
    assert report["backend"] == "local"
    assert report["steps"][:3] == ["begin", "ask", "resume"]
    assert report["steps"][-1] == "release_answer"
    assert report["answers"] == ["Sales grew 4%."]
