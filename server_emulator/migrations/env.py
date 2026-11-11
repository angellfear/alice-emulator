"""Окружение Alembic для всех веток (одна схема запуска — online, без autogenerate).
render_as_batch берётся из config.attributes — будущие ALTER на SQLite идут batch."""
from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def run_migrations_online():
    engine = create_engine(config.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection,
                          render_as_batch=config.attributes.get("render_as_batch", True))
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
