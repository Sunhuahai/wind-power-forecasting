import numpy as np

def metrics(actual,prediction):
    error=np.asarray(prediction)-np.asarray(actual)
    mae=float(np.mean(np.abs(error)));rmse=float(np.sqrt(np.mean(error**2)))
    # Capacity is 1 in the supplied normalized units, not an invented MW rating.
    return dict(MAE=mae,RMSE=rmse,nMAE=mae,nRMSE=rmse,n=len(error))

def pooled_metrics(rows):
    """Pool per-site errors using actual sample counts, not equal site weights."""
    n=sum(r['n'] for r in rows)
    if not n: raise ValueError('No scored samples')
    mae=sum(r['MAE']*r['n'] for r in rows)/n
    rmse=float(np.sqrt(sum(r['RMSE']**2*r['n'] for r in rows)/n))
    return dict(MAE=mae,RMSE=rmse,nMAE=mae,nRMSE=rmse,n=n)
