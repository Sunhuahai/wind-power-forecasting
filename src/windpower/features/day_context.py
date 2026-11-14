"""Summary of one complete forecast bundle, never observed future weather/power."""
import numpy as np
import polars as pl

COLUMNS=['forecast_mean24','forecast_std24','forecast_min24','forecast_max24','forecast_trend24']


def day_context(panel):
    panel=panel.select('origin','valid_at','speed100').sort('origin','valid_at')
    speed=panel['speed100'].to_numpy()
    if not np.isfinite(speed).all() or (speed<0).any():
        raise ValueError('Forecast speeds must be finite and nonnegative')
    seconds=panel.select((pl.col('valid_at')-pl.col('origin')).dt.total_seconds().alias('seconds'))['seconds']
    counts=panel.group_by('origin').agg(pl.col('valid_at').n_unique().alias('n'))
    if not counts['n'].eq(24).all() or not seconds.is_between(3600,86400).all() or not (seconds%3600).eq(0).all():
        raise ValueError('Day context requires complete hourly forecast origins')
    return panel.group_by('origin').agg(
        pl.col('speed100').mean().alias(COLUMNS[0]),
        pl.col('speed100').std(ddof=0).alias(COLUMNS[1]),
        pl.col('speed100').min().alias(COLUMNS[2]),
        pl.col('speed100').max().alias(COLUMNS[3]),
        (pl.col('speed100').last()-pl.col('speed100').first()).alias(COLUMNS[4]),
    ).sort('origin')


def attach_context(frame,context):
    out=frame.join(context,on='origin',how='left',validate='m:1').sort('origin','valid_at')
    if not np.isfinite(out.select(COLUMNS).to_numpy()).all():
        raise ValueError('Missing or nonfinite day context')
    return out
