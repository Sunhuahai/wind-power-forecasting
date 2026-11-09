from datetime import timedelta
import numpy as np
import polars as pl
from sqlalchemy import text
from windpower.db.core import engine
A=['u10','v10','u100','v100']
B=A+['speed10','speed100','dir10_sin','dir10_cos','dir100_sin','dir100_cos','speed100_cube','shear','hour_sin','hour_cos','year_sin','year_cos','lead']
C=B+['lag1','lag2','lag24','mean24','std24','min24','max24','trend']
GROUPS={'A':A,'B':B,'C':C}

def feature_row(valid, weather, power):
    from windpower.data.ingest import origin_for
    origin=origin_for(valid)
    # Exact clock lookups: missing observations cannot silently shift a lag.
    hist=np.array([power.get(origin-timedelta(hours=k),np.nan) for k in range(1,25)],float)
    if not np.isfinite(hist).all() or not np.isfinite(list(weather.values())).all(): return None
    r=dict(weather); r.update(origin=origin, valid_at=valid,lead=int((valid-origin).total_seconds()/3600))
    for height in [10,100]:
        u,v=r[f'u{height}'],r[f'v{height}']; speed=np.hypot(u,v)
        r[f'speed{height}']=speed
        # Direction toward which vector points, encoded without an angular discontinuity.
        r[f'dir{height}_sin']=v/max(speed,1e-8);r[f'dir{height}_cos']=u/max(speed,1e-8)
    r['speed100_cube']=r['speed100']**3
    r['shear']=np.log(max(r['speed100'],1e-6)/max(r['speed10'],1e-6))/np.log(10)
    for name, angle in [('hour',2*np.pi*valid.hour/24),('year',2*np.pi*(valid.timetuple().tm_yday-1)/365.25)]:
        r[name+'_sin']=np.sin(angle); r[name+'_cos']=np.cos(angle)
    r.update(lag1=hist[0],lag2=hist[1],lag24=hist[23],mean24=hist.mean(),std24=hist.std(),min24=hist.min(),max24=hist.max(),trend=hist[0]-hist[23])
    return r

def build(site, labeled=True):
    with engine().connect() as c:
        power={r.observed_at: r.power if r.power is not None else np.nan for r in c.execute(text('SELECT observed_at,power FROM power_observations WHERE farm_id=:s'),{'s':site})}
        rows=c.execute(text('SELECT valid_at,u10,v10,u100,v100 FROM nwp_forecasts WHERE farm_id=:s ORDER BY valid_at'),{'s':site}).mappings().all()
    samples=[]
    for row in rows:
        t=row['valid_at']; w={k:row[k] if row[k] is not None else np.nan for k in A}
        r=feature_row(t,w,power)
        if r is None: continue
        y=power.get(t,np.nan)
        if labeled and not np.isfinite(y): continue
        r['actual']=y;samples.append(r)
    if not samples: raise ValueError('No valid samples in PostgreSQL')
    f=pl.DataFrame(samples)
    # Identical complete 24h origins for all models and feature groups.
    complete=f.group_by('origin').len().filter(pl.col('len')==24).select('origin')
    return f.join(complete,on='origin').sort(['origin','lead'])

def split(frame, start, end):
    # Target interval (start,end]; origin may never precede split start.
    return frame.filter((pl.col('origin')>=start)&(pl.col('valid_at')<=end)&(pl.col('valid_at')>start))
