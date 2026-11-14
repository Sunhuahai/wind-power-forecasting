from datetime import datetime,timedelta
import polars as pl
import pytest
from windpower.features.day_context import day_context,attach_context,COLUMNS


def test_forecast_context_stays_in_its_origin_and_ignores_targets():
    start=datetime(2013,1,1)
    rows=[dict(origin=start+timedelta(days=d),valid_at=start+timedelta(days=d,hours=h),
               speed100=float(h) if d==0 else 100.,actual=.99)
          for d in range(2) for h in range(1,25)]
    panel=pl.DataFrame(rows)
    context=day_context(panel)
    assert set(context.columns)==set(['origin']+COLUMNS)
    joined=attach_context(panel.select('origin','valid_at'),context)
    assert joined['forecast_mean24'][23]==12.5
    assert joined['forecast_max24'][23]==24.
    assert joined['forecast_trend24'][23]==23.
    assert joined['forecast_min24'][24]==100.
    assert day_context(panel.with_columns(pl.lit(0.).alias('actual'))).equals(context)
    with pytest.raises(ValueError,match='complete hourly'):
        day_context(panel.head(23))
    missing=panel.with_columns(pl.when(pl.col('valid_at')==start+timedelta(hours=1))
        .then(None).otherwise(pl.col('speed100')).alias('speed100'))
    with pytest.raises(ValueError,match='finite'):
        day_context(missing)
