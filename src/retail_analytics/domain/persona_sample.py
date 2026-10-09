"""The fixed sample findings every persona preview is built from.

A preview shows what a persona changes while the analysis stays the same:
current and proposed persona see these identical findings, and the preview
checks that every figure, evidence reference and limitation survived. The
figures are fictional and unrelated to any company data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SampleFinding:
    text: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class SampleReport:
    question: str
    definition: str
    findings: tuple[SampleFinding, ...]
    limitations: tuple[str, ...]
    # Every token a rendering must keep, in any wording around them.
    required: tuple[str, ...]

    def missing_from(self, rendering: str) -> tuple[str, ...]:
        lowered = rendering.casefold()
        return tuple(
            token for token in self.required if token.casefold() not in lowered
        )


SAMPLE_REPORT = SampleReport(
    question="How did sample revenue change from August to September?",
    definition=(
        "Revenue is completed item sales (item status exactly 'Complete'), "
        "dated by order date."
    ),
    findings=(
        SampleFinding("Revenue was 1,284,550.25 in September.", "ev-sample-1"),
        SampleFinding("That is 8.4% above August (1,184,900.10).", "ev-sample-2"),
        SampleFinding("Product 4417 contributed 12.6% of the increase.", "ev-sample-3"),
    ),
    limitations=(
        "September is a partial month: 28 complete days.",
        "Currency is declared by the operator, not verified from data.",
    ),
    required=(
        "1,284,550.25",
        "8.4%",
        "1,184,900.10",
        "4417",
        "12.6%",
        "ev-sample-1",
        "ev-sample-2",
        "ev-sample-3",
        "partial month",
        "28 complete days",
        "not verified",
        "completed item sales",
    ),
)
