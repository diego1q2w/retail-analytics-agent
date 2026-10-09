"""Analytical skills: core tools first, skills narrow the authorized catalog
and never widen it.

Uses the real composition-root registry (every capability and ``load_skill``)
and every server-role combination, so the checks hold for whatever the
catalog contains, not for a hand-written list.
"""

from __future__ import annotations

import itertools
import re

from retail_analytics.application.contracts.skills import SkillSegment
from retail_analytics.application.investigation_policy import (
    LOAD_SKILL,
    render_investigation_policy,
)
from retail_analytics.application.skill_assets import BUNDLED
from retail_analytics.application.tool_focus import (
    CURRENT,
    LEGACY_LOADERS,
    SKILL_IDS,
    SKILL_TOOLS,
    available_skills,
    focus_catalog,
    select_tools,
)
from retail_analytics.capabilities.tool_focus import LoadSkillInput
from retail_analytics.domain.access import Permission
from tests.unit.tools.test_instruction_tool_references import (
    _execution,
    _registry,
    _role_combinations,
)

FULL = frozenset(p.value for p in Permission)
ANALYSIS_ONLY = frozenset({Permission.ANALYSIS_READ.value})
# Reports without deletion (a token-narrowed executive scope).
NO_DELETE = frozenset(
    {Permission.ANALYSIS_READ.value, Permission.REPORTS_READ_OWN.value}
)
CORE = frozenset(
    {"execute_analysis", "fetch_evidence", "list_relations", "describe_relation"}
)
_ACTIVE_STATES: tuple[dict[str, int], ...] = (
    {},
    *({skill: 1} for skill in SKILL_IDS),
    dict.fromkeys(SKILL_IDS, 1),
)
_TOOL_LIKE = re.compile(r"\b[a-z]+_[a-z_]+\b")


def _authorized(permissions: frozenset[str]) -> list[str]:
    return [d.name for d in _registry().catalog(_execution(permissions))]


def test_the_catalog_has_four_skills_covering_every_specialized_tool() -> None:
    assert SKILL_IDS == (
        "investigation",
        "saved_reports",
        "preferences",
        "currency_conversion",
    )
    registered = set(_registry().names)
    # 4 core + 13 specialized business tools + the load_skill control tool.
    assert registered == CORE | SKILL_TOOLS | {LOAD_SKILL}
    assert len(SKILL_TOOLS) == 13
    # Each specialized tool belongs to exactly one skill.
    owners = [t for s in CURRENT.values() for t in s.tools]
    assert len(owners) == len(set(owners))


def test_skill_assets_are_versioned_and_name_only_gated_tools() -> None:
    for versions in BUNDLED:
        numbers = [s.version for s in versions]
        assert numbers == sorted(set(numbers)), versions[0].skill_id
        assert len({s.skill_id for s in versions}) == 1
        for skill in versions:
            for segment in skill.segments:
                named = {
                    n
                    for n in _TOOL_LIKE.findall(segment.text)
                    if n in SKILL_TOOLS | CORE
                }
                # A segment names a tool only if it is shown just with it.
                assert named <= segment.any_of, (skill.skill_id, named)


def test_ordinary_request_starts_with_core_tools_and_the_catalog() -> None:
    authorized = _authorized(FULL)
    selection = select_tools(authorized)
    assert selection.tools == CORE | {LOAD_SKILL}
    assert selection.focus.active == ()
    assert selection.focus.loadable == SKILL_IDS
    policy = render_investigation_policy(selection.tools, selection.prompt)
    for hidden in SKILL_TOOLS:
        assert hidden not in policy
    for skill in SKILL_IDS:
        assert f"- {skill}: " in policy
    assert "<skill " not in policy
    # Not mandatory: a figure is answered without loading anything.
    assert "answer directly without loading a skill" in policy
    assert "You cannot save reports" not in policy


def test_each_skill_exposes_its_authorized_tools_and_instructions_once() -> None:
    authorized = _authorized(FULL)
    for skill in CURRENT.values():
        selection = select_tools(authorized, {skill.skill_id: skill.version})
        assert skill.tools <= selection.tools, skill.skill_id
        assert selection.tools - skill.tools == CORE | {LOAD_SKILL}
        assert selection.focus.active == ((skill.skill_id, skill.version),)
        assert skill.skill_id not in selection.focus.loadable
        policy = render_investigation_policy(selection.tools, selection.prompt)
        tag = f'<skill name="{skill.skill_id}" version="{skill.version}">'
        assert policy.count(tag) == 1
        assert f"- {skill.skill_id}: " not in policy
    # Mixed requests compose skills.
    both = select_tools(authorized, {"saved_reports": 1, "currency_conversion": 1})
    assert {"save_report", "convert_currency"} <= both.tools
    assert "remember_preference" not in both.tools


def test_runs_that_loaded_t26_f5_groups_keep_them() -> None:
    from retail_analytics.application.tool_focus import effective_skills
    from tests.unit.tools.test_skill_activations import record

    ops = [record(loader) for loader in LEGACY_LOADERS]
    assert effective_skills(ops) == {
        "saved_reports": 1,
        "preferences": 1,
        "currency_conversion": 1,
    }


def test_partially_authorized_skill_omits_what_the_user_cannot_do() -> None:
    selection = select_tools(_authorized(NO_DELETE), {"saved_reports": 1})
    assert "save_report" in selection.tools
    assert "propose_report_deletion" not in selection.tools
    ((_, _, text),) = selection.prompt.active
    assert "propose_report_deletion" not in text
    assert "For deletion" not in text
    assert "You cannot delete reports for this user" in text
    assert "propose_report_deletion" not in render_investigation_policy(
        selection.tools, selection.prompt
    )
    # Analysts without reviewed examples keep the investigation guidance.
    no_examples = [n for n in _authorized(FULL) if n != "find_analysis_examples"]
    investigation = select_tools(no_examples, {"investigation": 1})
    ((_, _, guidance),) = investigation.prompt.active
    assert "find_analysis_examples" not in guidance
    assert "not available to you" in guidance


def test_selection_never_exceeds_the_authorized_catalog() -> None:
    registry = _registry()
    for (name, permissions), active in itertools.product(
        _role_combinations(), _ACTIVE_STATES
    ):
        authorized = {d.name for d in registry.catalog(_execution(permissions))}
        selection = select_tools(authorized, active)
        assert selection.tools <= authorized, (
            name,
            sorted(selection.tools - authorized),
        )
        offered = {s.skill_id for s in available_skills(authorized)}
        # The catalog lists only skills with something usable.
        assert {k for k, _ in selection.prompt.catalog} <= offered, name
        assert {k for k, _ in selection.focus.active} <= offered, name
        if not offered:
            assert LOAD_SKILL not in selection.tools, name


def test_analysis_only_never_sees_report_or_deletion_skills() -> None:
    authorized = _authorized(ANALYSIS_ONLY)
    offered = {s.skill_id for s in available_skills(authorized)}
    assert "saved_reports" not in offered
    # Even a run that recorded the skill earlier (access narrowed since).
    selection = select_tools(authorized, dict.fromkeys(SKILL_IDS, 1))
    assert not selection.tools & {
        "propose_report_deletion",
        "save_report",
        "list_reports",
    }
    assert "saved_reports" not in dict(selection.focus.active)


def test_load_skill_schema_offers_only_available_skill_ids() -> None:
    registry = _registry()
    full = focus_catalog(registry.catalog(_execution(FULL)))
    (loader,) = (d for d in full if d.name == LOAD_SKILL)
    assert loader.parameters["properties"]["name"]["enum"] == list(SKILL_IDS)  # type: ignore[call-overload,index]
    narrowed = focus_catalog(registry.catalog(_execution(ANALYSIS_ONLY)))
    (loader,) = (d for d in narrowed if d.name == LOAD_SKILL)
    enum = loader.parameters["properties"]["name"]["enum"]  # type: ignore[call-overload,index]
    assert "saved_reports" not in enum  # type: ignore[operator]
    # No path, prompt, tool list or code argument.
    assert set(LoadSkillInput.model_json_schema()["properties"]) == {"name"}


def test_segments_render_by_exposed_tools() -> None:
    segment = SkillSegment("x", any_of=frozenset({"a"}), none_of=frozenset({"b"}))
    assert segment.applies(frozenset({"a"}))
    assert not segment.applies(frozenset({"a", "b"}))
    assert not segment.applies(frozenset())


def test_saves_tokens_on_ordinary_requests() -> None:
    """Character count of the schemas sent, not provider tokens."""
    descriptors = _registry().catalog(_execution(FULL))
    exposed = select_tools([d.name for d in descriptors]).tools

    def size(names: frozenset[str] | set[str]) -> int:
        return sum(
            len(d.description) + len(str(d.parameters))
            for d in descriptors
            if d.name in names
        )

    everything = {d.name for d in descriptors} - {LOAD_SKILL}
    assert size(exposed) < size(everything) / 2
