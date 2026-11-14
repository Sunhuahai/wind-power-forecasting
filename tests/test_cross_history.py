from datetime import datetime,timedelta
from windpower.features.cross_history import history_panel


def test_exact_history_and_no_current_or_future_labels():
    origin=datetime(2013,1,1)
    obs={site:{origin-timedelta(hours=h):h/10 for h in range(1,7)} for site in range(1,11)}
    before=history_panel([origin],obs)
    for lookup in obs.values():
        lookup[origin]=.99
        lookup[origin+timedelta(hours=1)]=.01
    assert history_panel([origin],obs).equals(before)
    assert before['history_1_lag1'][0]==.1
    assert before['history_1_lag3'][0]==.3
    del obs[1][origin-timedelta(hours=1)]
    changed=history_panel([origin],obs)
    assert changed['history_1_lag1'][0] is None
    assert changed['history_1_lag3'][0]==.3
    assert changed['history_1_mean6'][0] is None
    assert changed['history_1_missing6'][0]==1.
