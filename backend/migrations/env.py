"""Alembic environment.

The DB URL is sourced from app.core.config.Settings (Pydantic-settings) so that
the same .env file drives both the FastAPI app and migrations. Alembic runs in
sync mode (asyncpg URL is converted to psycopg by Settings.database_url_sync).
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Imported so the Vector type is registered on Base.metadata before autogenerate
# walks it. Without this, the pgvector column type may render as NullType.
import pgvector.sqlalchemy  # noqa: F401

from app.core.config import settings
from app.models.schema import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Pydantic-settings is the single source of truth for the DB URL. Override what
# the .ini may carry (we leave it blank in alembic.ini for safety).
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
