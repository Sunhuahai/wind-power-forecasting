"""Cross-site power history at exact clocks strictly before forecast origin."""
from datetime import timedelta
import numpy as np
import polars as pl

FIELDS = ['lag1','lag3','mean6','trend6','missing6']
COLUMNS = [f'history_{site}_{name}' for site in range(1,11) for name in FIELDS]


def history_panel(origins, observations):
    rows=[]
    for origin in sorted(set(origins)):
        row=dict(origin=origin)
        for site in range(1,11):
            lookup=observations.get(site,{})
            values=np.array([lookup.get(origin-timedelta(hours=h),np.nan) for h in range(1,7)],dtype=float)
            finite=np.isfinite(values)
            features=dict(lag1=values[0],lag3=values[2],
                          mean6=float(values.mean()) if finite.all() else np.nan,
                          trend6=float(values[0]-values[-1]) if finite.all() else np.nan,
                          missing6=float((~finite).sum()))
            for name,value in features.items():
                row[f'history_{site}_{name}']=float(value) if np.isfinite(value) else None
        rows.append(row)
    return pl.DataFrame(rows,schema_overrides={name:pl.Float64 for name in COLUMNS})


def attach_history(frame,panel):
    out=frame.join(panel,on='origin',how='left',validate='m:1').sort('origin','valid_at')
    indicators=[f'history_{site}_missing6' for site in range(1,11)]
    if out.select(pl.any_horizontal(pl.col(indicators).is_null())).to_series().any():
        raise ValueError('History origin is missing from panel')
    return out
