"""Bundle inference from PostgreSQL rows (P2).

Reads only what a real forecast day would offer: the NWP bundle for the
requested origins and power observations strictly before each origin.
Components and composition weights come from the versioned bundle manifest,
never from code. Repeated runs upsert the same (bundle, farm, origin) rows.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import polars as pl

from windpower.features.build import build
from windpower.features.cross_nwp import attach_temporal, temporal_spatial_panel
from windpower.models import kpower_reproduction as kp
from windpower.serving.bundle import load_bundle

SITES = tuple(range(1, 11))


def load_artifacts(bundle, component_id: str) -> dict:
    """joblib artifacts for one manifest component, keyed by site or 'all'."""
    spec = bundle.components[component_id]
    return {key: joblib.load(bundle.root / meta['path'])
            for key, meta in spec['files'].items()}


def stage1_models(bundle) -> dict:
    """The ten stage-one models used by every pooled ablation script.

    All later components load stage-one from the `full` reproduction bundle,
    so serving must use the identical source to reproduce the chain.
    """
    return load_artifacts(bundle, 'full')['1']['stage1']


def assemble(bundle, sites=SITES, origins=None) -> dict[int, pl.DataFrame]:
    """Feature frames per site: base + neighborhood + stage1 cross + spatio-temporal.

    `origins` (list of datetime) restricts the output rows; panels and history
    are still read whole so exact-key joins behave like the training chain.
    """
    stage1 = stage1_models(bundle)
    panels = {s: kp.nwp_panel(s) for s in SITES}
    crosses = []
    for s in SITES:
        pred = np.clip(stage1[s].predict(panels[s].select(kp.STAGE1).to_numpy()), 0, 1)
        crosses.append(panels[s].select('origin', 'valid_at')
                       .with_columns(pl.Series(f'stage1_{s}', pred)))
    cross_future = kp.combine(crosses)
    spatial = temporal_spatial_panel(panels)
    frames = {}
    for s in sites:
        frame = kp.attach(build(s, labeled=False), panels[s])
        frame = kp.add_cross_site(frame, cross_future)
        frame = attach_temporal(frame, spatial)
        if origins is not None:
            frame = frame.filter(pl.col('origin').is_in(origins))
        if frame.is_empty():
            raise ValueError(f'site {s}: no complete origins in request')
        frames[s] = frame.sort('origin', 'valid_at')
    return frames


def _mean_predict(models, matrix) -> np.ndarray:
    return np.mean([np.clip(m.predict(matrix), 0, 1) for m in models], axis=0)


def component_values(bundle, frames: dict[int, pl.DataFrame]) -> dict[int, dict[str, np.ndarray]]:
    """Per-site, per-component predictions on the assembled frames."""
    artifacts = {cid: load_artifacts(bundle, cid) for cid in bundle.component_ids()}
    curves = artifacts['curve']['all']['curves']
    values = {}
    for s, frame in frames.items():
        row = {}
        for cid in ('C', 'neighbors', 'full'):
            art = artifacts[cid][str(s)]
            row[cid] = np.clip(art['point'].predict(
                frame.select(bundle.components[cid]['columns']).to_numpy()), 0, 1)
        for cid in ('spatial', 'temporal'):
            art = artifacts[cid][str(s)]
            row[cid] = _mean_predict(art['models'],
                                     frame.select(bundle.components[cid]['columns']).to_numpy())
        pooled = frame.with_columns(pl.lit(s).alias('farm'))
        shared_cols = bundle.components['shared']['columns']
        row['shared'] = np.clip(artifacts['shared']['all']['model'].predict(
            pooled.select(shared_cols).to_numpy()), 0, 1)
        curve_cols = bundle.components['curve']['columns']
        curve = curves[s].predict(frame['speed100'].to_numpy())
        curve_frame = pooled.with_columns(pl.Series('curve', curve))
        residual = artifacts['curve']['all']['model'].predict(
            curve_frame.select(curve_cols).to_numpy())
        row['curve'] = np.clip(curve + residual, 0, 1)
        mlp_cols = bundle.components['mlp']['columns']
        design = np.column_stack([frame.select(mlp_cols).to_numpy(),
                                  np.eye(10)[s - 1].reshape(1, -1).repeat(frame.height, axis=0)])
        row['mlp'] = _mean_predict(artifacts['mlp']['all']['models'], design)
        values[s] = row
    return values


def infer(bundle_path='models/wind_v1', sites=SITES, origins=None,
          dry_run=False, write_csv=None) -> pl.DataFrame:
    """Compose the bundle over assembled features; upsert predictions idempotently."""
    bundle = load_bundle(bundle_path)
    report = bundle.verify()
    broken = {cid: r for cid, r in report.items() if r['missing'] or r['mismatched']}
    if broken:
        raise ValueError(f'bundle integrity failed: {broken}')
    frames = assemble(bundle, sites, origins)
    values = component_values(bundle, frames)
    outputs = []
    for s, frame in frames.items():
        composed = bundle.compose(values[s])
        out = frame.select('origin', 'valid_at', 'lead').with_columns(
            pl.Series('prediction', composed),
            *[pl.Series(cid, values[s][cid]) for cid in sorted(values[s])])
        out = out.with_columns(pl.lit(s).alias('farm'))
        outputs.append(out)
    result = pl.concat(outputs).sort(['farm', 'origin', 'valid_at'])
    if dry_run:
        return result
    from windpower.db.core import engine, upsert
    from windpower.db.schema import model_runs, predictions
    with engine().begin() as c:
        for s in sites:
            run_id = f"bundle:{bundle.version}:site:{s}"
            upsert(c, model_runs, [dict(
                id=run_id, farm_id=s, model='serving_bundle', feature_group=bundle.version,
                created_at=datetime.now(),
                config=dict(protocol=bundle.manifest['protocol'],
                            components=bundle.component_ids()),
                artifact=str(bundle.root), metrics=None)])
            rows = [dict(run_id=run_id, origin=r['origin'], valid_at=r['valid_at'],
                         lead=r['lead'], prediction=float(r['prediction']), actual=None)
                    for r in result.filter(pl.col('farm') == s).iter_rows(named=True)]
            upsert(c, predictions, rows)
    if write_csv:
        result.write_csv(write_csv)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', default='models/wind_v1')
    parser.add_argument('--origin', action='append', default=None,
                        help='origin timestamp, e.g. 2013-07-01T00:00:00; repeatable')
    parser.add_argument('--sites', default='all', help='comma list or all')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--csv', default=None, help='also write predictions to this CSV')
    args = parser.parse_args()
    sites = SITES if args.sites == 'all' else tuple(int(s) for s in args.sites.split(','))
    origins = ([datetime.fromisoformat(o) for o in args.origin]
               if args.origin else None)
    result = infer(args.bundle, sites, origins, dry_run=args.dry_run, write_csv=args.csv)
    per_farm = result.group_by('farm').agg(pl.col('prediction').len().alias('hours'))
    print(json.dumps(dict(rows=result.height, farms=per_farm.sort('farm').to_dicts(),
                          dry_run=args.dry_run), default=str, indent=2))


if __name__ == '__main__':
    main()
