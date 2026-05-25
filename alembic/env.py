"""Alembic env — connects to ipeds DB and isolates migrations to the `landing` schema."""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make src importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from landing_scraper.config import settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DB_URL = (
    f"postgresql+psycopg://{settings.postgres_user}"
    f"{':' + settings.postgres_password if settings.postgres_password else ''}"
    f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
)

config.set_main_option("sqlalchemy.url", DB_URL)

target_metadata = None  # raw SQL migrations


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "schema":
        return name == "landing"
    return getattr(obj, "schema", None) == "landing"


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="landing",
        include_schemas=True,
        include_object=include_object,
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
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS landing")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="landing",
            include_schemas=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
