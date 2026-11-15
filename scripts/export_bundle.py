"""Export the accepted strict-protocol chain as a versioned serving bundle.

The manifest stores component artifact paths (relative to the bundle), checksums,
feature columns and the composition tree as data, so inference code never needs
to know the branch structure.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib

from windpower.serving.bundle import sha256

ROOT = Path(__file__).resolve().parents[1]
ID = 'ablation_20261114_v1'
ABL = ROOT / 'artifacts/kpower_ablation' / ID
REPRO = ROOT / 'artifacts/kpower_reproduction'


def rel(path: Path, base: Path) -> str:
    import os
    return os.path.relpath(str(path.resolve()), str(base.resolve()))


def file_entry(path: Path, base: Path) -> dict:
    return dict(path=rel(path, base), sha256=sha256(path), bytes=path.stat().st_size)


def main():
    out = ROOT / 'models/wind_v1'
    out.mkdir(parents=True, exist_ok=True)
    weights = {name: json.loads((ABL / name / 'weights.json').read_text())['weights']
               if name == 'validation_blend'
               else json.loads((ABL / name / 'weights.json').read_text())
               for name in ('validation_blend', 'spatial_nwp_v1', 'temporal_nwp_v1',
                            'shared_model_v1', 'curve_residual_v1', 'mlp_seedavg_tuned_v1')}
    vb, sp, tp, sh, cv, ml = (weights[k] for k in ('validation_blend', 'spatial_nwp_v1',
                                                   'temporal_nwp_v1', 'shared_model_v1',
                                                   'curve_residual_v1', 'mlp_seedavg_tuned_v1'))
    components = {}
    for name in ('C', 'neighbors', 'full'):
        files, columns = {}, None
        for site in range(1, 11):
            artifact = next((REPRO / f'{ID}_{name}' / f'site_{site}').glob('*.joblib'))
            files[str(site)] = file_entry(artifact, out)
            if columns is None:
                columns = joblib.load(artifact)['stage2_columns']
        components[name] = dict(kind='per_site_lgbm', stage='stage2', files=files, columns=columns)
    for name, folder in (('spatial', 'spatial_nwp_v1'), ('temporal', 'temporal_nwp_v1')):
        files, columns = {}, None
        for site in range(1, 11):
            artifact = ABL / folder / f'site_{site}.joblib'
            files[str(site)] = file_entry(artifact, out)
            if columns is None:
                columns = joblib.load(artifact)['columns']
        components[name] = dict(kind='per_site_lgbm', stage='stage2', files=files, columns=columns)
    for name, folder in (('shared', 'shared_model_v1'), ('curve', 'curve_residual_v1'), ('mlp', 'mlp_seedavg_tuned_v1')):
        artifact = ABL / folder / 'model.joblib'
        bundle = joblib.load(artifact)
        spec = dict(kind='pooled_model', files={'all': file_entry(artifact, out)},
                    columns=bundle['columns'])
        if name == 'curve':
            spec['extra'] = 'adds the per-site isotonic curve value before the residual model'
        if name == 'mlp':
            spec['extra'] = 'needs one-hot farm identity appended; ensemble of '
            spec['members'] = len(bundle['models'])
        components[name] = spec

    def comp(cid):
        return dict(op='component', id=cid)

    def add(*terms):
        return dict(op='add', terms=[dict(coef=float(c), node=n) for c, n in terms])

    tree = add(
        (ml['baseline'], add(
            (cv['baseline'], add(
                (sh['baseline'], add(
                    (tp['baseline'], add(
                        (sp['baseline'], add((vb['C'], comp('C')),
                                             (vb['neighbors'], comp('neighbors')),
                                             (vb['full'], comp('full')))),
                        (sp['spatial'], comp('spatial')))),
                    (tp['temporal'], comp('temporal')))),
                (sh['shared'], comp('shared')))),
            (cv['curve_residual'], comp('curve')))),
        (ml['mlp_seedavg'], comp('mlp')))
    manifest = dict(
        bundle_version='wind_v1',
        created_at=datetime.now(timezone.utc).isoformat(),
        protocol=('strict: components trained on 2012 OOF rows; validation 2013 H1 used for '
                  'early stopping and convex weights only; test 2013 H2 scored once'),
        registry_milestone='mlp_seedavg_tuned',
        components=components,
        compose=dict(op='clip', lo=0.0, hi=1.0, node=tree),
        weights=dict(validation_blend=vb, spatial=sp, temporal=tp, shared=sh, curve=cv, mlp=ml),
        notes=('per-site components are keyed by zone id; pooled components carry a single artifact; '
               'mlp needs one-hot farm identity and averages its members'))
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(f'wrote {out / "manifest.json"}')
    print('components:', sorted(components))


if __name__ == '__main__':
    main()
