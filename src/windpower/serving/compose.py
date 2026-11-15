"""Evaluate a composition tree of component predictions.

The tree is data, not code: weights and branch structure live in the bundle
manifest, so a new combination never requires a code change.
"""
from __future__ import annotations

import numpy as np

OPS = ('add', 'component', 'clip')


def validate_tree(node, component_ids):
    """Raise ValueError unless the tree is well formed and references known components."""
    if not isinstance(node, dict) or node.get('op') not in OPS:
        raise ValueError(f'Unknown node: {node!r}')
    op = node['op']
    if op == 'component':
        if node.get('id') not in component_ids:
            raise ValueError(f'Unknown component: {node.get("id")!r}')
        return
    if op == 'clip':
        lo, hi = float(node['lo']), float(node['hi'])
        if not lo < hi:
            raise ValueError('clip bounds must be increasing')
        validate_tree(node['node'], component_ids)
        return
    terms = node.get('terms')
    if not terms:
        raise ValueError('add node needs at least one term')
    for term in terms:
        if 'coef' not in term:
            raise ValueError('each term needs a coef')
        validate_tree(term['node'], component_ids)


def compose(node, values):
    """Return the composed prediction for a mapping of component id -> ndarray."""
    op = node['op']
    if op == 'component':
        return np.asarray(values[node['id']], dtype=float)
    if op == 'clip':
        return np.clip(compose(node['node'], values), float(node['lo']), float(node['hi']))
    total = None
    for term in node['terms']:
        part = float(term['coef']) * compose(term['node'], values)
        total = part if total is None else total + part
    return total
