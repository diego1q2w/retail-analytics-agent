"""PostgreSQL ``RunEventStore`` (also a ``ProgressSink``).

Appending locks the run row and takes the next value of its event counter, so
sequences are gap-free and strictly ordered per run even with concurrent
publishers. Replay is by sequence after the client's last received event ID.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.runs import lock_run
from retail_analytics.adapters.postgres.schema import run_events, runs
from retail_analytics.application.persistence import RecordNotFound
from retail_analytics.application.progress import ProgressEvent, ProgressUpdate


def _event(row: sa.Row[tuple[object, ...]]) -> ProgressEvent:
    m = row._mapping
    return ProgressEvent.stamp(
        ProgressUpdate.model_validate(m["payload"]),
        event_id=m["event_id"],
        sequence=m["sequence"],
        occurred_at=m["occurred_at"],
    )


class PostgresRunEventStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, update: ProgressUpdate) -> ProgressEvent:
        return await self._db.transaction(self._append, update)

    async def publish(self, update: ProgressUpdate) -> None:
        await self.append(update)

    def _append(
        self, connection: sa.Connection, update: ProgressUpdate
    ) -> ProgressEvent:
        run_id = update.correlation.run_id
        run = lock_run(connection, run_id)
        if run.session_id != update.correlation.session_id:
            # An event must not be filed under another session's run.
            raise RecordNotFound("run", run_id)
        sequence = connection.execute(
            sa.update(runs)
            .where(runs.c.run_id == run_id)
            .values(last_event_sequence=runs.c.last_event_sequence + 1)
            .returning(runs.c.last_event_sequence)
        ).scalar_one()
        event = ProgressEvent.stamp(
            update,
            event_id=self._db.new_id(),
            sequence=sequence,
            occurred_at=self._db.clock(),
        )
        connection.execute(
            sa.insert(run_events).values(
                event_id=event.event_id,
                run_id=run_id,
                sequence=sequence,
                kind=update.kind.value,
                payload=update.model_dump(mode="json"),
                occurred_at=event.occurred_at,
            )
        )
        return event

    async def replay(
        self, run_id: str, *, after_event_id: str | None = None, limit: int = 500
    ) -> Sequence[ProgressEvent]:
        return await self._db.transaction(self._replay, run_id, after_event_id, limit)

    @staticmethod
    def _replay(
        connection: sa.Connection,
        run_id: str,
        after_event_id: str | None,
        limit: int,
    ) -> list[ProgressEvent]:
        after = 0
        if after_event_id is not None:
            found = connection.execute(
                sa.select(run_events.c.sequence).where(
                    run_events.c.run_id == run_id,
                    run_events.c.event_id == after_event_id,
                )
            ).scalar_one_or_none()
            if found is None:
                raise RecordNotFound("run event", after_event_id)
            after = found
        rows = connection.execute(
            sa.select(run_events)
            .where(run_events.c.run_id == run_id, run_events.c.sequence > after)
            .order_by(run_events.c.sequence)
            .limit(limit)
        ).all()
        return [_event(row) for row in rows]
