import json
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
