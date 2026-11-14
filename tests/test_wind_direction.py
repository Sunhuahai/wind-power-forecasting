from datetime import datetime,timedelta
import numpy as np
import polars as pl
import pytest
from windpower.features.wind_direction import direction_panel,DIRECTION_COLUMNS
from windpower.features.cross_nwp import temporal_spatial_panel


def test_rotation_sign_calm_and_origin_boundary():
    start=datetime(2013,1,1)
    rows=[]
    for day in range(2):
        for h in range(1,25):
            u,v=(1.,0.) if day==0 else (0.,1.)
            if day==0 and h==2:u,v=0.,1.
            if day==1 and h==24:u,v=0.,0.
            rows.append(dict(origin=start+timedelta(days=day),valid_at=start+timedelta(days=day,hours=h),u100=u,v100=v,speed100=float(np.hypot(u,v))))
    panels={site:pl.DataFrame(rows) for site in range(1,11)}
    result=direction_panel(panels)
    original=temporal_spatial_panel(panels)
    assert result.select(original.columns).equals(original)
    assert set(result.columns)-set(original.columns)==set(DIRECTION_COLUMNS)
    assert result['nwp_1_turn_sin'][0]==pytest.approx(1.)
    assert result['nwp_1_turn_sin'][1]==pytest.approx(-1.)
    assert result['nwp_1_turn_sin'][23]==pytest.approx(0.)
    assert result['nwp_1_turn_cos'][23]==pytest.approx(1.)
    assert result['nwp_1_dir_sin'][47]==0.
    assert np.isfinite(result.select(DIRECTION_COLUMNS).to_numpy()).all()
