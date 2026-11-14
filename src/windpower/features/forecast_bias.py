"""Past forecast-response errors, with exact clocks strictly before origin."""
from datetime import timedelta
import numpy as np
import polars as pl

PAST=['past_nwp_speed1','past_nwp_speed3','past_nwp_mean6','past_curve_error1','past_curve_error_mean6','past_bias_missing6']
COLUMNS=PAST+['future_curve','forecast_speed_change1','decayed_curve_error1']


def bias_panel(origins,power,speed,curve):
    rows=[]
    for origin in sorted(set(origins)):
        times=[origin-timedelta(hours=h) for h in range(1,7)]
        wind=np.array([speed.get(t,np.nan) for t in times],dtype=float)
        observed=np.array([power.get(t,np.nan) for t in times],dtype=float)
        finite=np.isfinite(wind)&(wind>=0)
        expected=np.full(6,np.nan)
        if finite.any():expected[finite]=curve.predict(wind[finite])
        error=observed-expected
        values=[wind[0],wind[2],wind.mean() if finite.all() else np.nan,error[0],
                error.mean() if np.isfinite(error).all() else np.nan,float((~np.isfinite(error)).sum())]
        rows.append(dict(origin=origin,**{k:float(v) if np.isfinite(v) else None for k,v in zip(PAST,values)}))
    return pl.DataFrame(rows,schema_overrides={name:pl.Float64 for name in PAST})


def attach_bias(frame,panel,curve):
    out=frame.join(panel,on='origin',how='left',validate='m:1').sort('origin','valid_at')
    if out['past_bias_missing6'].null_count():raise ValueError('Missing forecast-bias origin')
    return out.with_columns(pl.Series('future_curve',curve.predict(out['speed100'].to_numpy())),
        (pl.col('speed100')-pl.col('past_nwp_speed1')).alias('forecast_speed_change1'),
        (pl.col('past_curve_error1')*(-(pl.col('lead')-1)/6).exp()).alias('decayed_curve_error1'))
