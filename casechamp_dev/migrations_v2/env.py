from alembic import context
from app.settings import Settings
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

settings = Settings()
engine = create_engine(settings.migration_database_url or settings.database_url, poolclass=NullPool)

if context.is_offline_mode():
    context.configure(url=engine.url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine.connect() as connection:
        context.configure(
            connection=connection, version_table_schema="public", version_table="alembic_version_v2"
        )
        with context.begin_transaction():
            context.run_migrations()
