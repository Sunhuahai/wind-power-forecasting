"""Direction and signed next-hour rotation within one forecast origin."""
import numpy as np
import polars as pl
from windpower.features.cross_nwp import temporal_spatial_panel,attach_spatial,COLUMNS,TEMPORAL_COLUMNS,KEYS

DIRECTION_COLUMNS=[f'nwp_{site}_{name}' for site in range(1,11)
                   for name in ['dir_sin','dir_cos','turn_sin','turn_cos']]


def direction_panel(panels):
    combined=temporal_spatial_panel(panels)
    for site in range(1,11):
        frame=panels[site].select(KEYS+['u100','v100']).sort(KEYS)
        u,v=frame['u100'].to_numpy(),frame['v100'].to_numpy()
        speed=np.maximum(np.hypot(u,v),1e-8)
        frame=frame.with_columns(pl.Series('cos',u/speed),pl.Series('sin',v/speed))
        frame=frame.with_columns(
            pl.col('cos').shift(-1).over('origin').fill_null(pl.col('cos')).alias('next_cos'),
            pl.col('sin').shift(-1).over('origin').fill_null(pl.col('sin')).alias('next_sin'))
        direction=frame.select(KEYS+[
            pl.col('sin').alias(f'nwp_{site}_dir_sin'),pl.col('cos').alias(f'nwp_{site}_dir_cos'),
            (pl.col('next_sin')*pl.col('cos')-pl.col('next_cos')*pl.col('sin')).alias(f'nwp_{site}_turn_sin'),
            (pl.col('next_cos')*pl.col('cos')+pl.col('next_sin')*pl.col('sin')).alias(f'nwp_{site}_turn_cos')])
        combined=combined.join(direction,on=KEYS,how='inner',validate='1:1')
    return combined.sort(KEYS)


def attach_directions(frame,panel):
    return attach_spatial(frame,panel,columns=COLUMNS+TEMPORAL_COLUMNS+DIRECTION_COLUMNS)
