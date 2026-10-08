"""Alembic environment for the application database.

The connection string comes from the typed backend settings
(``RETAIL_ANALYTICS_DATABASE_URL`` or ``.env``), never from alembic.ini. Run as
the application role: it owns the database, and Temporal's databases are
unreachable to it by design.

Offline mode (``alembic upgrade head --sql``) renders SQL without a connection.
"""

from alembic import context
from sqlalchemy import create_engine, pool

from retail_analytics.bootstrap.config import ConfigError, load_backend_settings

config = context.config
# Schema is written as explicit migration operations; there is no ORM metadata to
# autogenerate from (domain records stay independent of SQLAlchemy).
target_metadata = None


def _database_url() -> str:
    try:
        url = load_backend_settings().database_url
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    if url is None:
        raise SystemExit("RETAIL_ANALYTICS_DATABASE_URL is required to migrate")
    return url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url="postgresql+psycopg://",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
