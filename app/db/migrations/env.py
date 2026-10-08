"""Alembic environment: uses the app's DATABASE_URL and model metadata."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from app.db.models import Base
from app.db.schema_filter import include_object as _include_object

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # called from app.db.migrate with an open connection
        context.configure(connection=connection, target_metadata=target_metadata, include_object=_include_object)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(config.get_main_option("sqlalchemy.url"))  # type: ignore[arg-type]
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, include_object=_include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
