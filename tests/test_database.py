"""Opt-in PostgreSQL integration checks, never use SQLite substitutes."""
import os
import pytest
from sqlalchemy import text
from windpower.db.core import engine,health
@pytest.mark.skipif(os.getenv('WINDPOWER_TEST_DB')!='1',reason='Set WINDPOWER_TEST_DB=1 with migrated PostgreSQL 18.6')
def test_real_database():
    assert health().startswith('18.6')
    with engine().connect() as c:
        assert c.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('wind_farms','power_observations','nwp_forecasts','dataset_splits','model_runs','predictions')"))==6
        assert c.scalar(text('SELECT count(*) FROM predictions WHERE lead NOT BETWEEN 1 AND 24'))==0
        assert c.scalar(text('SELECT count(*) FROM predictions WHERE valid_at <= origin'))==0
