"""PostgreSQL ``PreferenceStore``.

Each public method is one transaction. It first locks the executive's row, so
all preference writes of one executive are serialized, then changes the
preference or proposal and appends its audit event together. Lifecycle rules
(version bumps, proposal transitions) come from the domain; this module only
loads, applies and stores them.
"""

from __future__ import annotations

import sqlalchemy as sa

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import (
    executives,
    preference_events,
    preference_proposals,
    user_preferences,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.persistence import RecordNotFound
from retail_analytics.application.contracts.preferences import (
    ObservationResult,
    ResolveResult,
    SaveResult,
)
from retail_analytics.domain.periods import OverrideScope
from retail_analytics.domain.preferences import (
    InferenceProposal,
    Preference,
    PreferenceSetting,
    PreferenceSource,
    ProposalStatus,
    parse_slot,
)

type Row = sa.Row[tuple[object, ...]]


def _setting(slot: str, value: str) -> PreferenceSetting:
    kind, term = parse_slot(slot)
    return PreferenceSetting(kind, value, term)


def _preference(row: Row) -> Preference:
    m = row._mapping
    return Preference(
        preference_id=m["preference_id"],
        executive_id=m["executive_id"],
        setting=_setting(m["slot"], m["value"]),
        scope=OverrideScope(m["scope"]),
        session_id=m["session_id"],
        source=PreferenceSource(m["source"]),
        version=m["version"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def _proposal(row: Row) -> InferenceProposal:
    m = row._mapping
    return InferenceProposal(
        proposal_id=m["proposal_id"],
        executive_id=m["executive_id"],
        session_id=m["session_id"],
        setting=_setting(m["slot"], m["value"]),
        observations=m["observations"],
        status=ProposalStatus(m["status"]),
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        expires_at=m["expires_at"],
    )


class PostgresPreferenceStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _lock_executive(self, connection: sa.Connection, executive_id: str) -> None:
        found = connection.execute(
            sa.select(executives.c.executive_id)
            .where(executives.c.executive_id == executive_id)
            .with_for_update()
        ).one_or_none()
        if found is None:
            raise RecordNotFound("executive", executive_id)

    def _event(
        self,
        connection: sa.Connection,
        executive_id: str,
        slot: str,
        action: str,
        *,
        scope: OverrideScope | None = None,
        session_id: str | None = None,
        source: PreferenceSource | None = None,
        version: int | None = None,
    ) -> None:
        connection.execute(
            sa.insert(preference_events).values(
                executive_id=executive_id,
                session_id=session_id,
                slot=slot,
                scope=None if scope is None else scope.value,
                action=action,
                source=None if source is None else source.value,
                version=version,
                occurred_at=self._db.clock(),
            )
        )

    @staticmethod
    def _find(
        connection: sa.Connection,
        executive_id: str,
        slot: str,
        scope: OverrideScope,
        session_id: str | None,
    ) -> Preference | None:
        query = sa.select(user_preferences).where(
            user_preferences.c.executive_id == executive_id,
            user_preferences.c.slot == slot,
            user_preferences.c.scope == scope.value,
        )
        if scope is OverrideScope.SESSION:
            query = query.where(user_preferences.c.session_id == session_id)
        row = connection.execute(query.with_for_update()).one_or_none()
        return None if row is None else _preference(row)

    def _save(
        self,
        connection: sa.Connection,
        executive_id: str,
        setting: PreferenceSetting,
        scope: OverrideScope,
        session_id: str | None,
        source: PreferenceSource,
        event: str | None,
    ) -> SaveResult:
        now = self._db.clock()
        previous = self._find(connection, executive_id, setting.slot, scope, session_id)
        if previous is None:
            created = Preference(
                preference_id=self._db.new_id(),
                executive_id=executive_id,
                setting=setting,
                scope=scope,
                session_id=session_id,
                source=source,
                version=1,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                sa.insert(user_preferences).values(
                    preference_id=created.preference_id,
                    executive_id=executive_id,
                    scope=scope.value,
                    session_id=session_id,
                    slot=setting.slot,
                    value=setting.value,
                    source=source.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            current, action = created, "remembered"
        elif (previous.setting, previous.source) == (setting, source):
            return SaveResult(previous, previous)
        else:
            current, action = previous.changed(setting, source, now), "changed"
            connection.execute(
                sa.update(user_preferences)
                .where(user_preferences.c.preference_id == previous.preference_id)
                .values(
                    value=setting.value,
                    source=source.value,
                    version=current.version,
                    updated_at=now,
                )
            )
        if source is PreferenceSource.INFERRED_SESSION:
            action = "adapted"
        self._event(
            connection,
            executive_id,
            setting.slot,
            event or action,
            scope=scope,
            session_id=session_id,
            source=source,
            version=current.version,
        )
        return SaveResult(current, previous)

    async def save(
        self,
        executive_id: str,
        setting: PreferenceSetting,
        scope: OverrideScope,
        session_id: str | None,
        source: PreferenceSource,
    ) -> SaveResult:
        return await self._db.transaction(
            self._save_locked, executive_id, setting, scope, session_id, source
        )

    def _save_locked(
        self,
        connection: sa.Connection,
        executive_id: str,
        setting: PreferenceSetting,
        scope: OverrideScope,
        session_id: str | None,
        source: PreferenceSource,
    ) -> SaveResult:
        self._lock_executive(connection, executive_id)
        return self._save(
            connection, executive_id, setting, scope, session_id, source, None
        )

    async def list_preferences(
        self, executive_id: str, session_id: str | None
    ) -> tuple[Preference, ...]:
        return await self._db.transaction(self._list, executive_id, session_id)

    @staticmethod
    def _list(
        connection: sa.Connection, executive_id: str, session_id: str | None
    ) -> tuple[Preference, ...]:
        scope_filter = user_preferences.c.scope == OverrideScope.USER_DEFAULT.value
        if session_id is not None:
            scope_filter = sa.or_(
                scope_filter, user_preferences.c.session_id == session_id
            )
        rows = connection.execute(
            sa.select(user_preferences)
            .where(user_preferences.c.executive_id == executive_id, scope_filter)
            .order_by(user_preferences.c.slot, user_preferences.c.scope)
        )
        return tuple(_preference(r) for r in rows)

    async def forget(
        self,
        executive_id: str,
        slot: str,
        scope: OverrideScope,
        session_id: str | None,
    ) -> Preference | None:
        return await self._db.transaction(
            self._forget, executive_id, slot, scope, session_id
        )

    def _forget(
        self,
        connection: sa.Connection,
        executive_id: str,
        slot: str,
        scope: OverrideScope,
        session_id: str | None,
    ) -> Preference | None:
        self._lock_executive(connection, executive_id)
        found = self._find(connection, executive_id, slot, scope, session_id)
        if found is None:
            return None
        connection.execute(
            sa.delete(user_preferences).where(
                user_preferences.c.preference_id == found.preference_id
            )
        )
        self._event(
            connection,
            executive_id,
            slot,
            "forgotten",
            scope=scope,
            session_id=session_id,
            version=found.version,
        )
        return found

    async def forget_all(self, executive_id: str) -> tuple[Preference, ...]:
        return await self._db.transaction(self._forget_all, executive_id)

    def _forget_all(
        self, connection: sa.Connection, executive_id: str
    ) -> tuple[Preference, ...]:
        self._lock_executive(connection, executive_id)
        rows = connection.execute(
            sa.delete(user_preferences)
            .where(user_preferences.c.executive_id == executive_id)
            .returning(*user_preferences.c)
        ).all()
        removed = tuple(_preference(r) for r in rows)
        connection.execute(
            sa.delete(preference_proposals).where(
                preference_proposals.c.executive_id == executive_id
            )
        )
        for pref in removed:
            self._event(
                connection,
                executive_id,
                pref.setting.slot,
                "forgotten",
                scope=pref.scope,
                session_id=pref.session_id,
                version=pref.version,
            )
        return removed

    async def observe(
        self, executive_id: str, session_id: str, setting: PreferenceSetting
    ) -> ObservationResult:
        return await self._db.transaction(
            self._observe, executive_id, session_id, setting
        )

    def _observe(
        self,
        connection: sa.Connection,
        executive_id: str,
        session_id: str,
        setting: PreferenceSetting,
    ) -> ObservationResult:
        self._lock_executive(connection, executive_id)
        now = self._db.clock()
        p = preference_proposals.c
        same = (
            p.executive_id == executive_id,
            p.slot == setting.slot,
            p.value == setting.value,
        )
        row = connection.execute(
            sa.select(preference_proposals)
            .where(*same, p.session_id == session_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            declined = connection.execute(
                sa.select(sa.literal(1))
                .select_from(preference_proposals)
                .where(*same, p.status == ProposalStatus.DECLINED.value)
                .limit(1)
            ).first()
            status = ProposalStatus.DECLINED if declined else ProposalStatus.OBSERVING
            proposal_id = self._db.new_id()
            connection.execute(
                sa.insert(preference_proposals).values(
                    proposal_id=proposal_id,
                    executive_id=executive_id,
                    session_id=session_id,
                    slot=setting.slot,
                    value=setting.value,
                    observations=0,
                    status=status.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            row = connection.execute(
                sa.select(preference_proposals).where(p.proposal_id == proposal_id)
            ).one()
        before = _proposal(row)
        after = before.observe(now)
        if after != before:
            connection.execute(
                sa.update(preference_proposals)
                .where(p.proposal_id == before.proposal_id)
                .values(
                    observations=after.observations,
                    status=after.status.value,
                    updated_at=now,
                    expires_at=after.expires_at,
                )
            )
        newly = (
            before.status is ProposalStatus.OBSERVING
            and after.status is ProposalStatus.PROPOSED
        )
        if newly:
            self._event(
                connection,
                executive_id,
                setting.slot,
                "proposed",
                session_id=session_id,
                source=PreferenceSource.INFERRED_SESSION,
            )
        return ObservationResult(after, newly)

    async def open_proposals(
        self, executive_id: str, session_id: str | None
    ) -> tuple[InferenceProposal, ...]:
        return await self._db.transaction(self._open, executive_id, session_id)

    def _open(
        self, connection: sa.Connection, executive_id: str, session_id: str | None
    ) -> tuple[InferenceProposal, ...]:
        p = preference_proposals.c
        query = sa.select(preference_proposals).where(
            p.executive_id == executive_id,
            p.status == ProposalStatus.PROPOSED.value,
            p.expires_at > self._db.clock(),
        )
        if session_id is not None:
            query = query.where(p.session_id == session_id)
        return tuple(
            _proposal(r) for r in connection.execute(query.order_by(p.created_at))
        )

    async def resolve_proposal(
        self, executive_id: str, proposal_id: str, *, confirm: bool
    ) -> ResolveResult:
        return await self._db.transaction(
            self._resolve, executive_id, proposal_id, confirm
        )

    def _resolve(
        self,
        connection: sa.Connection,
        executive_id: str,
        proposal_id: str,
        confirm: bool,
    ) -> ResolveResult:
        self._lock_executive(connection, executive_id)
        p = preference_proposals.c
        row = connection.execute(
            sa.select(preference_proposals)
            .where(p.proposal_id == proposal_id, p.executive_id == executive_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise AccessDenied("proposal", proposal_id)
        now = self._db.clock()
        proposal = _proposal(row)
        resolved = proposal.confirm(now) if confirm else proposal.decline(now)
        connection.execute(
            sa.update(preference_proposals)
            .where(p.proposal_id == proposal_id)
            .values(status=resolved.status.value, updated_at=now)
        )
        saved: Preference | None = None
        if confirm:
            saved = self._save(
                connection,
                executive_id,
                proposal.setting,
                OverrideScope.USER_DEFAULT,
                None,
                PreferenceSource.INFERRED_CONFIRMED,
                "confirmed",
            ).preference
        else:
            self._event(
                connection,
                executive_id,
                proposal.setting.slot,
                "declined",
                session_id=proposal.session_id,
                source=PreferenceSource.INFERRED_SESSION,
            )
        return ResolveResult(resolved, saved)
