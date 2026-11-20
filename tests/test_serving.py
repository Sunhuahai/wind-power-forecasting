import json
import os
from pathlib import Path

import numpy as np
import pytest

from windpower.serving.bundle import load_bundle, sha256
from windpower.serving.compose import compose, validate_tree

ROOT = Path(__file__).resolve().parents[1]


def test_compose_linear_and_clip():
    values = {'a': np.array([0.2, 0.9]), 'b': np.array([0.1, 0.1])}
    tree = {'op': 'clip', 'lo': 0.0, 'hi': 1.0,
            'node': {'op': 'add', 'terms': [{'coef': 0.5, 'node': {'op': 'component', 'id': 'a'}},
                                            {'coef': 0.5, 'node': {'op': 'component', 'id': 'b'}}]}}
    validate_tree(tree, {'a', 'b'})
    assert np.allclose(compose(tree, values), [0.15, 0.5])


def test_compose_clips_out_of_range():
    tree = {'op': 'clip', 'lo': 0.0, 'hi': 1.0, 'node': {'op': 'component', 'id': 'a'}}
    assert np.allclose(compose(tree, {'a': np.array([-0.3, 1.4])}), [0.0, 1.0])


def test_validate_rejects_unknown_component():
    with pytest.raises(ValueError):
        validate_tree({'op': 'component', 'id': 'nope'}, {'a'})


def test_validate_rejects_term_without_coef():
    with pytest.raises(ValueError):
        validate_tree({'op': 'add', 'terms': [{'node': {'op': 'component', 'id': 'a'}}]}, {'a'})


def test_bundle_roundtrip_and_checksum(tmp_path):
    component = tmp_path / 'c.npy'
    np.save(component, np.zeros(3))
    manifest = dict(bundle_version='test', created_at='2026-01-01', protocol='unit test',
                    components={'a': {'kind': 'pooled_model', 'columns': ['x'],
                                      'files': {'all': {'path': 'c.npy', 'sha256': sha256(component)}}}},
                    compose={'op': 'component', 'id': 'a'})
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    bundle = load_bundle(tmp_path)
    assert bundle.component_ids() == ['a']
    assert bundle.verify()['a']['mismatched'] == []
    component.write_bytes(b'corrupted')
    assert bundle.verify()['a']['mismatched'] == ['all']


def test_exported_bundle_validates():
    path = ROOT / 'models/wind_v1'
    if not (path / 'manifest.json').exists():
        pytest.skip('serving bundle not exported')
    bundle = load_bundle(path)
    assert set(bundle.component_ids()) == {'C', 'neighbors', 'full', 'spatial',
                                           'temporal', 'shared', 'curve', 'mlp'}
    composed = bundle.compose({cid: np.array([0.4, 0.6]) for cid in bundle.component_ids()})
    assert np.allclose(composed, [0.4, 0.6])  # weights sum to one, so identical inputs pass through


def test_bundle_artifacts_verify():
    path = ROOT / 'models/wind_v1'
    if not (path / 'manifest.json').exists():
        pytest.skip('serving bundle not exported')
    bundle = load_bundle(path)
    report = bundle.verify()
    assert all(not r['missing'] and not r['mismatched'] for r in report.values())


@pytest.mark.skipif(os.getenv('WINDPOWER_TEST_DB') != '1',
                    reason='Set WINDPOWER_TEST_DB=1 with migrated PostgreSQL 18.6')
def test_serving_single_origin_matches_replay():
    """The serving path reproduces the frozen chain on one test origin.

    Engineering equivalence check on an already-scored, frozen period;
    no selection or tuning happens here.
    """
    frozen_path = ROOT / 'artifacts/kpower_ablation/ablation_20261114_v1' \
        / 'mlp_seedavg_tuned_v1/predictions.csv'
    if not frozen_path.exists():
        pytest.skip('frozen chain predictions not present')
    import polars as pl
    from windpower.serving.predict import infer
    frozen = pl.read_csv(frozen_path, try_parse_dates=True)
    origin = frozen['origin'].unique().sort().to_list()[0]
    site = int(frozen['site'].unique().sort().to_list()[0])
    result = infer(sites=(site,), origins=[origin], dry_run=True)
    expected = frozen.filter((pl.col('origin') == origin) & (pl.col('site') == site))
    assert result.height == 24
    joined = expected.select('valid_at', 'lead',
                             pl.col('mlp_seedavg_blend').alias('frozen')).sort('lead')
    got = result.select('valid_at', 'lead', 'prediction').sort('lead')
    assert joined['valid_at'].to_list() == got['valid_at'].to_list()
    assert float((joined['frozen'] - got['prediction']).abs().max()) <= 1e-9
