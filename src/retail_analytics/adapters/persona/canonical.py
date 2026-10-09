"""Offline preview renderer: the sample findings in a fixed, neutral layout.

It never calls a model, so it cannot apply free-text style. It proves the
preview plumbing (same findings for the current and proposed persona, numbers,
evidence and limitations intact) and shows the exact instruction block the
model would receive. A model-backed renderer implements the same port.
"""

from __future__ import annotations

from retail_analytics.domain.persona_sample import SampleReport


class CanonicalPreviewRenderer:
    applies_persona = False

    async def render(self, instructions: str | None, sample: SampleReport) -> str:
        del instructions
        lines = [sample.question, "", "Findings:"]
        lines.extend(f"- {f.text} [{f.evidence_id}]" for f in sample.findings)
        lines.extend(["", f"Definition: {sample.definition}", "", "Limitations:"])
        lines.extend(f"- {limitation}" for limitation in sample.limitations)
        return "\n".join(lines)
