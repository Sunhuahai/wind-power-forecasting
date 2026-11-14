from datetime import datetime,timedelta
import numpy as np
import polars as pl
import pytest
from windpower.features.forecast_bias import bias_panel,attach_bias,COLUMNS


class Curve:
    def predict(self,speed):return np.asarray(speed)/10


def test_bias_uses_exact_past_forecasts_and_power():
    origin=datetime(2013,1,1)
    power={origin-timedelta(hours=h):.5 for h in range(1,7)}
    wind={origin-timedelta(hours=h):3. for h in range(1,7)}
    before=bias_panel([origin],power,wind,Curve())
    power[origin]=1.; wind[origin]=100.
    power[origin+timedelta(hours=1)]=0.;wind[origin+timedelta(hours=1)]=0.
    assert bias_panel([origin],power,wind,Curve()).equals(before)
    frame=pl.DataFrame(dict(origin=[origin,origin],valid_at=[origin+timedelta(hours=h) for h in [1,24]],lead=[1,24],speed100=[4.,4.]))
    result=attach_bias(frame,before,Curve())
    assert result['past_curve_error1'][0]==pytest.approx(.2)
    assert result['decayed_curve_error1'][0]==pytest.approx(.2)
    assert result['decayed_curve_error1'][1]==pytest.approx(.2*np.exp(-23/6))
    assert result['future_curve'].to_list()==[.4,.4]
    assert set(COLUMNS)<=set(result.columns)
    del wind[origin-timedelta(hours=1)]
    missing=bias_panel([origin],power,wind,Curve())
    assert missing['past_nwp_speed1'][0] is None
    assert missing['past_nwp_speed3'][0]==3.
    assert missing['past_bias_missing6'][0]==1.
