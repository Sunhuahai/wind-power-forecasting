import numpy as np
import pytest
from windpower.models.lead_blend import predict_lead_blend,groups


def test_six_hour_boundaries_and_global_fallback():
    leads=[1,6,7,12,13,18,19,24]
    x=np.tile([0.,1.],(8,1))
    weights={0:np.array([1.,0.]),1:np.array([0.,1.]),2:np.array([.25,.75]),3:np.array([.75,.25])}
    assert predict_lead_blend(x,leads,[.5,.5],weights,1)==pytest.approx([0,0,1,1,.75,.75,.25,.25])
    assert predict_lead_blend(x,leads,[.5,.5],weights,0)==pytest.approx([.5]*8)
    with pytest.raises(ValueError,match='integer 1..24'):groups([0,25])
