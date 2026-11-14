"""Forecast-only first-stage means with expanding, strictly past-label OOF."""
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor
from windpower.models.kpower_reproduction import STAGE1,MIN_TRAIN_DAYS,OOF_BLOCK_DAYS,OOF_TREES

COLUMNS=[f'stage1_mean_{site}' for site in range(1,11)]


def estimator(params,trees=None):
    settings=dict(params,objective='regression')
    if trees is not None:settings['n_estimators']=trees
    return LGBMRegressor(**settings)


def mean_oof(train,panel,params,site):
    origins=train['origin'].unique().sort().to_list()
    blocks=[]
    for start in range(MIN_TRAIN_DAYS,len(origins),OOF_BLOCK_DAYS):
        cutoff=origins[start]
        tr=train.filter((pl.col('origin')<cutoff)&(pl.col('valid_at')<cutoff))
        hold=origins[start:start+OOF_BLOCK_DAYS]
        va=panel.filter(pl.col('origin').is_in(hold))
        if tr.is_empty() or va.is_empty():continue
        model=estimator(params,OOF_TREES)
        model.fit(tr.select(STAGE1).to_numpy(),tr['actual'].to_numpy())
        blocks.append(va.select('origin','valid_at').with_columns(pl.Series(f'stage1_mean_{site}',
            np.clip(model.predict(va.select(STAGE1).to_numpy()),0,1))))
    if not blocks:raise ValueError('Not enough training days for mean OOF')
    return pl.concat(blocks).sort('origin','valid_at')
