from alembic import context
from sqlalchemy import create_engine

config = context.config
engine = create_engine(config.attributes["admin_url"])
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
