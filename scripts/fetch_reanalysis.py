"""Fetch NASA POWER hourly reanalysis for the ten zones once coordinates are known.

Reads configs/sites.json. Refuses to run while any coordinate is null. Caches one
JSON per zone, then writes a tidy CSV keyed by zone and source-clock timestamp.
"""
import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'configs/sites.json'
RAW = ROOT / 'data/external/power_raw'
OUT = ROOT / 'data/external/reanalysis_hourly.csv'
BASE = 'https://power.larc.nasa.gov/api/temporal/hourly/point'


def load_sites(config=CONFIG):
    cfg = json.loads(Path(config).read_text())
    sites = cfg['sites']
    missing = [z for z, v in sites.items() if v.get('latitude') is None or v.get('longitude') is None]
    if missing:
        raise SystemExit(f'coordinates missing for zones {missing}; fill configs/sites.json first')
    return cfg['reanalysis'], sites


def fetch(zone, site, params, start, end, timeout=120):
    query = urllib.parse.urlencode(dict(
        parameters=','.join(params), community='RE', longitude=site['longitude'],
        latitude=site['latitude'], start=start, end=end, format='JSON'))
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / f'zone{zone}_{start}_{end}.json'
    if cache.exists():
        return json.loads(cache.read_text())
    for attempt in range(4):
        try:
            with urllib.request.urlopen(f'{BASE}?{query}', timeout=timeout) as response:
                payload = json.loads(response.read().decode())
            cache.write_text(json.dumps(payload))
            return payload
        except Exception as error:  # network hiccup: back off and retry
            if attempt == 3:
                raise
            wait = 5 * (attempt + 1)
            print(f'zone {zone}: {error}; retrying in {wait}s', flush=True)
            time.sleep(wait)


def to_rows(zone, site, payload):
    params = payload['properties']['parameter']
    offset = timedelta(hours=float(site.get('utc_offset_hours', 0)))
    rows = []
    for stamp, values in params['T2M'].items():
        if values in (None, -999.0):
            continue
        utc = datetime.strptime(stamp, '%Y%m%d%H')
        row = dict(zone=int(zone), observed_at=(utc + offset).isoformat(sep=' '))
        for name, series in params.items():
            value = series.get(stamp)
            row[name.lower()] = None if value in (None, -999.0) else float(value)
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='20120101')
    parser.add_argument('--end', default='20131231')
    parser.add_argument('--dry-run', action='store_true', help='validate config only')
    parser.add_argument('--config', default=str(CONFIG), help='path to a sites config')
    args = parser.parse_args()
    meta, sites = load_sites(args.config)
    if args.dry_run:
        print(f'config ok: {len(sites)} zones, parameters {meta["parameters"]}')
        return
    rows = []
    for zone, site in sites.items():
        payload = fetch(zone, site, meta['parameters'], args.start, args.end)
        zone_rows = to_rows(zone, site, payload)
        rows.extend(zone_rows)
        print(f'zone {zone}: {len(zone_rows)} hours', flush=True)
    frame = pl.DataFrame(rows).sort(['zone', 'observed_at'])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frame.write_csv(OUT)
    print(f'wrote {OUT} rows={frame.height} cols={frame.columns}')


if __name__ == '__main__':
    main()
