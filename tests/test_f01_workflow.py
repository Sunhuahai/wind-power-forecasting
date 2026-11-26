import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, event
from typer.testing import CliRunner

from tests.synthetic_scada import STEP, T0, frame, record, tiny_config
from windpower.cli import app
from windpower.features import scada_samples
from windpower.models import scada_baseline


def test_baseline_and_validation_cli_only_load_registered_pretest_rows(tmp_path, monkeypatch):
    config_path = tiny_config(tmp_path / 'experiment.json')
    config = json.loads(config_path.read_text())
    config['dataset'] = dict(file_sha256='synthetic-test',
                             time_semantics='naive', interval_start_end_semantics='unconfirmed')
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(scada_baseline, 'CONFIG_PATH', config_path)
    monkeypatch.setattr(scada_baseline, '_protocol_tests_passed', lambda: False)
    manifest_engine = MagicMock()
    monkeypatch.setattr(scada_baseline, 'engine', manifest_engine)
    monkeypatch.chdir(tmp_path)

    # 只为 SELECT 建合成表，数据库快照写入单独用替身记录。
    db = create_engine('sqlite://')
    all_rows = [dict(turbine_id=1, **r) for r in frame(700).to_dicts()]
    all_rows += [dict(turbine_id=1, **record(575, file_id='other-version')),
                 dict(turbine_id=2, **record(575, grid_active_power_kw=-999))]
    table = Table('turbine_scada_10m', MetaData(), *(
        Column(c.name, c.type) for c in scada_samples.turbine_scada_10m.c
        if c.name in all_rows[0]
    ))
    table.metadata.create_all(db)
    with db.begin() as conn:
        conn.execute(table.insert(), all_rows)
    monkeypatch.setattr(scada_samples, 'engine', lambda: db)
    reads = []

    @event.listens_for(db, 'before_cursor_execute')
    def capture_read(conn, cursor, statement, parameters, context, executemany):
        reads.append((statement, parameters))

    runner = CliRunner()
    out_dir = tmp_path / 'artifacts' / 'f01-validation'
    output_option = ['--out-dir', str(out_dir)]
    result = runner.invoke(app, ['baseline-f01'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['gate']['status'] == 'fail'
    summary = json.loads((out_dir / 'summary.json').read_text())
    cutoff = T0 + 576 * STEP
    assert summary['coverage']['observed_before'] == config['splits']['validation'][1]
    assert summary['coverage']['splits']['validation']['target_max'] == str(cutoff - STEP)
    assert summary['coverage']['splits']['validation']['rows'] == 861
    assert summary['coverage']['splits']['test']['rows'] is None
    assert summary['test_set']['rows'] is None
    before = {p: p.read_bytes() for p in out_dir.rglob('*') if p.is_file()}

    replay = runner.invoke(app, ['final-test-f01', '--split', 'validation', *output_option])
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.stdout) == {
        name: {metric: round(metrics[metric], 4) for metric in ('MAE', 'RMSE')}
        for name, metrics in summary['models'].items()
    }
    assert before == {p: p.read_bytes() for p in out_dir.rglob('*') if p.is_file()}
    assert len(reads) == 2
    for sql, params in reads:
        assert 'turbine_scada_10m.observed_at <' in sql
        assert 'turbine_scada_10m.file_id =' in sql
        assert params == (1, 'synthetic-test', cutoff.isoformat(sep=' ', timespec='microseconds'))
    manifest_engine.assert_called_once()


def test_cli_preserves_test_evidence_and_checks_consumption_before_loading(tmp_path, monkeypatch):
    def fail_on_load(*args, **kwargs):
        pytest.fail('测试集门禁应在读取样本之前结束')

    monkeypatch.setattr(scada_baseline, 'load_turbine_frame', fail_on_load)
    runner = CliRunner()
    baseline = runner.invoke(app, ['baseline-f01', '--out-dir', 'artifacts/f01'])
    assert baseline.exit_code == 1
    assert '一次性测试证据' in baseline.output
    score = runner.invoke(app, ['final-test-f01', '--split', 'test', '--out-dir', str(tmp_path)])
    assert score.exit_code == 1
    assert '已完成一次性评分' in score.output
    assert not list(tmp_path.iterdir())
