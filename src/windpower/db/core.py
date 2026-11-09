import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert
load_dotenv()
def engine():
    url = os.getenv('DATABASE_URL', 'postgresql+psycopg://windpower:windpower_local@127.0.0.1:55432/windpower')
    if not url.startswith('postgresql'):
        raise ValueError('Only PostgreSQL is supported')
    return create_engine(url)
def health():
    with engine().connect() as conn:
        version = conn.scalar(text('SHOW server_version'))
    if not version.startswith('18.6'):
        raise RuntimeError(f'Expected PostgreSQL 18.6, got {version}')
    return version
def upsert(conn, table, rows):
    for start in range(0, len(rows), 1000):
        stmt = insert(table).values(rows[start:start+1000])
        keys = [c.name for c in table.primary_key]
        conn.execute(stmt.on_conflict_do_update(index_elements=keys,
            set_={c.name: stmt.excluded[c.name] for c in table.columns if c.name not in keys}))
