"""Paired calendar-day circular block bootstrap, shared across all farms."""
from datetime import timedelta
import numpy as np
import polars as pl

def paired_comparison(frame, candidate, baseline='baseline', replicates=2000, block_days=7, seed=42):
    """Wide frame, one row per identical (farm,origin,valid_at) prediction sample.

    Resample calendar days in blocks; all farms and all hours of a selected day
    travel together. Unobserved days keep count zero; do not compress time gaps.
    """
    if replicates < 1 or block_days < 1: raise ValueError('Invalid bootstrap settings')
    daily = frame.with_columns(
        (pl.col(baseline)-pl.col('actual')).abs().alias('ba'),
        (pl.col(candidate)-pl.col('actual')).abs().alias('ca'),
        (pl.col(baseline)-pl.col('actual')).pow(2).alias('bs'),
        (pl.col(candidate)-pl.col('actual')).pow(2).alias('cs'),
    ).group_by('origin').agg(pl.len().alias('n'),*[pl.col(k).sum() for k in ['ba','ca','bs','cs']])
    lookup={r['origin']:r for r in daily.iter_rows(named=True)}
    start,end=frame['origin'].min(),frame['origin'].max()
    calendar=[start+timedelta(days=i) for i in range((end-start).days+1)]
    values=np.array([[lookup[d][k] if d in lookup else 0 for k in ['n','ba','ca','bs','cs']] for d in calendar],float)
    n,ba,ca,bs,cs=values.sum(axis=0)
    delta_mae=(ca-ba)/n;delta_rmse=np.sqrt(cs/n)-np.sqrt(bs/n)
    rng=np.random.default_rng(seed);days=len(calendar)
    starts=rng.integers(0,days,size=(replicates,int(np.ceil(days/block_days))))
    indices=((starts[:,:,None]+np.arange(block_days))%days).reshape(replicates,-1)[:,:days]
    sums=values[indices].sum(axis=1);valid=sums[:,0]>0;sums=sums[valid]
    mae=(sums[:,2]-sums[:,1])/sums[:,0]
    rmse=np.sqrt(sums[:,4]/sums[:,0])-np.sqrt(sums[:,3]/sums[:,0])
    return dict(candidate=candidate,baseline=baseline,n=int(n),calendar_days=days,
                delta_MAE=float(delta_mae),delta_RMSE=float(delta_rmse),
                delta_MAE_low=float(np.quantile(mae,.025)),delta_MAE_high=float(np.quantile(mae,.975)),
                delta_RMSE_low=float(np.quantile(rmse,.025)),delta_RMSE_high=float(np.quantile(rmse,.975)),
                bootstrap_fraction_RMSE_better=float(np.mean(rmse<0)),
                replicates=int(len(sums)),block_days=block_days,seed=seed)
