from alembic import context
from windpower.db.core import engine
from windpower.db.schema import metadata
with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
