"""Read only the official nested Task 15 archives; retain raw source timestamps."""
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO
from datetime import datetime, timedelta
import json
import math
import urllib.request
import polars as pl
from windpower.db.core import engine, upsert
from windpower.db.schema import wind_farms, power_observations, nwp_forecasts
URL = 'https://www.dropbox.com/s/pqenrr2mcvl0hk9/GEFCom2014.zip?dl=1'
RAW = Path('data/raw')
ASSUMPTION = ('Daily 00:00 source-clock origin; following 1..24h NWP assumed available at origin. '
              'Competition replay assumption, actual issue/arrival unknown; timezone unknown.')
def download():
    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW/'GEFCom2014.zip'
    if not dest.exists():
        temp = dest.with_suffix('.part')
        urllib.request.urlretrieve(URL, temp)
        with ZipFile(temp) as z:
            if z.testzip(): raise ValueError('Corrupt ZIP')
        temp.replace(dest)
    with ZipFile(dest) as z:
        n = next(n for n in z.namelist() if n.endswith('GEFCom2014-W_V2.zip'))
        (RAW/'GEFCom2014-W_V2.zip').write_bytes(z.read(n))
    return dest

def timestamp(value):
    return datetime.strptime(value, '%Y%m%d %H:%M')
def origin_for(valid):
    return (valid - timedelta(hours=1)).replace(hour=0, minute=0)
def clean(value, power=False):
    if value is None: return None, 'missing'
    v = float(value)
    if not math.isfinite(v): return None, 'nonfinite'
    if power and not 0 <= v <= 1: return None, 'outside_0_1'
    return v, None

def read_site(site):
    with ZipFile(RAW/'GEFCom2014-W_V2.zip') as w:
        frames=[]
        for kind in ['Task15', 'TaskExpVars15']:
            path=f'Wind/Task 15/{kind}_W_Zone1_10.zip'
            with ZipFile(BytesIO(w.read(path))) as z:
                name=next(n for n in z.namelist() if n.endswith(f'_Zone{site}.csv'))
                b=z.read(name); (RAW/Path(name).name).write_bytes(b)
                frames.append(pl.read_csv(BytesIO(b), null_values=["NA", "NaN", ""]))
        solution_bytes=w.read('Wind/Solution to Task 15/solution15_W.csv')
        (RAW/'solution15_W.csv').write_bytes(solution_bytes)
        sol=pl.read_csv(BytesIO(solution_bytes), null_values=["NA", "NaN", ""]).filter(pl.col('ZONEID') == site)
        (RAW/'Instructions.txt').write_bytes(w.read('Wind/Instructions.txt'))
    history, future=frames
    future=future.join(sol, on=['ZONEID','TIMESTAMP'], how='left')
    return pl.concat([history, future.select(history.columns)], how='vertical')

def ingest(site):
    if not 1 <= site <= 10: raise ValueError('site must be 1..10')
    frame=read_site(site)
    duplicate=frame.height-frame.unique(['ZONEID','TIMESTAMP']).height
    if duplicate:
        raise ValueError(f'{duplicate} duplicate source keys; resolve conflicts before import')
    powers=[]; forecasts=[]; issues={}
    for row in frame.iter_rows(named=True):
        t=timestamp(row['TIMESTAMP']); flags={}
        p, flag=clean(row['TARGETVAR'], True)
        if flag: flags['power']=flag
        weather={}; wf={}
        for key in ['U10','V10','U100','V100']:
            weather[key.lower()], f=clean(row[key])
            if f: wf[key]=f
        for key, value in {**flags,**wf}.items():
            k=f'{key}:{value}'; issues[k]=issues.get(k,0)+1
        powers.append(dict(farm_id=site, observed_at=t, power=p, raw_timestamp=row['TIMESTAMP'],quality=flags))
        forecasts.append(dict(farm_id=site, valid_at=t, assumed_origin=origin_for(t), issued_at=None,
            raw_timestamp=row['TIMESTAMP'], quality=wf, availability=ASSUMPTION, **weather))
    times=sorted(p['observed_at'] for p in powers)
    gaps=[str(b) for a,b in zip(times,times[1:]) if b-a != timedelta(hours=1)]
    report=dict(site=site, rows=frame.height, duplicates=duplicate, start=str(times[0]), end=str(times[-1]),
                gap_count=len(gaps), gaps=gaps[:20], field_issues=issues, timezone='unknown', availability=ASSUMPTION)
    with engine().begin() as c:
        upsert(c, wind_farms, [dict(id=site,name=f'GEFCom2014 Zone {site}',power_unit='normalized_0_1',time_semantics='source naive time, timezone unknown')])
        upsert(c, power_observations, powers); upsert(c,nwp_forecasts,forecasts)
    out=Path(f'artifacts/site_{site}');out.mkdir(parents=True,exist_ok=True)
    (out/'quality_source.json').write_text(json.dumps(report,indent=2))
    return report
