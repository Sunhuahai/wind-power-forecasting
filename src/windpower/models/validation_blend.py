"""Convex prediction blend fitted only on supplied validation predictions/labels."""
import numpy as np
from scipy.optimize import minimize


def fit_weights(predictions, actual):
    x = np.asarray(predictions, dtype=float)
    y = np.asarray(actual, dtype=float)
    if x.ndim != 2 or y.ndim != 1 or len(x) != len(y) or not x.size:
        raise ValueError("Expected nonempty validation matrix and aligned labels")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("Validation data must be finite")
    count = x.shape[1]
    result = minimize(
        lambda weights: np.mean((x @ weights - y) ** 2),
        np.full(count, 1 / count),
        jac=lambda weights: 2 * x.T @ (x @ weights - y) / len(y),
        method="SLSQP", bounds=[(0., 1.)] * count,
        constraints=[dict(type="eq", fun=lambda weights: weights.sum() - 1,
                          jac=lambda weights: np.ones(count))],
        options=dict(ftol=1e-14, maxiter=1000),
    )
    if not result.success:
        raise RuntimeError(f"Validation blend fit failed: {result.message}")
    weights = np.clip(result.x, 0, 1)
    return weights / weights.sum()
