import numpy as np
import pytest
from windpower.models.validation_blend import fit_weights


def test_recovers_validation_mixture_and_rejects_bad_input():
    x = np.random.default_rng(42).random((200, 3))
    expected = np.array([.2, .3, .5])
    weights = fit_weights(x, x @ expected)
    assert weights == pytest.approx(expected, abs=1e-6)
    assert weights.sum() == pytest.approx(1)
    assert np.all(weights >= 0)
    with pytest.raises(ValueError, match="finite"):
        fit_weights([[np.nan]], [0])
    with pytest.raises(ValueError, match="aligned"):
        fit_weights(x, [0])
