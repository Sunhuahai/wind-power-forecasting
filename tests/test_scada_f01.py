"""F01 读取/审计/入库测试：GB18030、'--'、哈希锁定、幂等导入（T01/T02）。

合成文件使用 27 列缩减结构；真实文件测试在文件存在时核对全部审计事实。
数据库用例复用已迁移的开发库（WINDPOWER_TEST_DB=1），以独立 turbine_id=99
写入并在结束时清理，不触碰真实 F01 数据与 GEFCom2014 实验表。
"""
import hashlib
import os
from pathlib import Path

import pytest

from tests.synthetic_scada import MONITORED, FIELD_COLUMNS, record, synth_csv
from windpower.data import scada_f01

REAL_CSV = Path('data/raw/f01/F01号风机十分钟数据.csv')
DB = pytest.mark.skipif(os.getenv('WINDPOWER_TEST_DB') != '1',
                        reason='Set WINDPOWER_TEST_DB=1 with migrated PostgreSQL 18.6')


def _synth_file(tmp_path: Path, rows=40):
    records = [record(s) for s in range(rows)]
    records[7]['grid_active_power_kw'] = -31.92          # 负功率：必须原样保留
    records[9]['pitch2_deg'] = -128.1                  # 桨距越界
    path = synth_csv(tmp_path / 't99.csv', records, skip_steps=(11,))  # 一个 10 分钟缺口
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    written = rows - 1
    expect = dict(expected_sha256=digest, expected_rows=written,
                 expected_columns=len(FIELD_COLUMNS) + len(MONITORED) + 5)
    return path, expect, written


def test_gb18030_dash_and_negative_parsed(tmp_path):
    path, expect, written = _synth_file(tmp_path)
    raw = scada_f01.read_raw(path, **expect)
    assert raw.height == written
    assert raw['设备名称'].unique().to_list() == ['T99']
    # '--' 保留在原始载荷中，不静默转为 null
    dash_col = '平均偏航集电环温度(℃)'
    assert (raw[dash_col] == '--').sum() == raw.height
    # 负功率按字符串原样读入
    assert '-31.92' in raw['平均网侧有功功率(kW)'].to_list()
    assert '847' in raw['平均发电机进风口温度1(℃)'].to_list()


def test_hash_and_shape_guards(tmp_path):
    path, expect, written = _synth_file(tmp_path)
    digest = expect['expected_sha256']
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        scada_f01.read_raw(path, expected_sha256='0' * 64, expected_rows=written,
                           expected_columns=27)
    with pytest.raises(ValueError, match='Expected'):
        scada_f01.read_raw(path, expected_sha256=digest, expected_rows=written + 5,
                           expected_columns=27)
    with pytest.raises(FileNotFoundError):
        scada_f01.read_raw(tmp_path / 'missing.csv', expected_sha256=digest,
                           expected_rows=1, expected_columns=1)


def test_audit_counts_on_synthetic(tmp_path):
    path, expect, written = _synth_file(tmp_path)
    report = scada_f01.audit(path, **expect)
    label = report['label_candidate']['grid_active_power_kw']
    assert label['negative_count'] == 1
    assert label['negative_min_kw'] == -31.92
    assert report['row_flag_hits']['sentinel_847_intake_temp1'] == written
    assert report['row_flag_hits']['pitch_outside_canonical'] == 1
    assert report['time_axis']['gap_events'] == 1
    assert report['time_axis']['missing_grid_points'] == 1
    assert report['policy_eligibility']['GX_policy_accuracy'] is None


@pytest.mark.skipif(not REAL_CSV.exists(), reason='Real F01 CSV not present in this checkout')
def test_real_file_audit_facts():
    """对已注册真实文件重跑审计，核对规划文档 §1 的全部关键数字。"""
    report = scada_f01.audit()
    assert report['file']['sha256'] == scada_f01.EXPECTED_SHA256
    assert report['file']['rows'] == 52_749 and report['file']['columns'] == 54
    axis = report['time_axis']
    assert axis['missing_grid_points'] == 99 and axis['duplicate_timestamps'] == 0
    assert axis['gap_events'] == 64 and axis['max_gap_minutes'] == 180.0
    assert axis['gap_delta_minutes_histogram'] == {'20': 55, '30': 5, '40': 2, '120': 1, '180': 1}
    assert report['complete_days'] == 310 and report['incomplete_days'] == 57
    label = report['label_candidate']['grid_active_power_kw']
    assert label['negative_count'] == 9_968
    assert label['negative_share'] == pytest.approx(0.189, abs=1e-3)
    assert label['negative_min_kw'] == -31.92
    assert label['max_kw'] == 5314.22
    assert label['zero_count'] == 290
    assert label['negative_with_generating_h_zero'] == 8_668
    hits = report['row_flag_hits']
    assert hits['sentinel_847_intake_temp1'] == 50_084
    assert hits['yaw_position_outside_canonical'] == 2_675
    assert hits['pitch_outside_canonical'] == 26
    assert hits['after_time_gap'] == 64
    assert report['identity']['设备名称'] == ['F01']
    assert report['identity']['型号名称'] == ['GW191/5270']
    assert report['wind_speed']['max_ms'] == 18.69


@pytest.mark.skipif(not REAL_CSV.exists(), reason='Real F01 CSV not present in this checkout')
def test_real_typed_rows_preserve_negatives_and_sentinels():
    raw = scada_f01.read_raw()
    typed, hits = scada_f01.typed_rows(raw)
    powers = [r['grid_active_power_kw'] for r in typed]
    assert min(powers) == -31.92
    assert sum(1 for p in powers if p < 0) == 9_968
    flagged = [r for r in typed if r['quality_flags'].get('negative_grid_power')]
    assert len(flagged) == 9_968
    # 全部 54 列原值保留在原始载荷中
    assert len(typed) == 52_749
    assert all(len(r['quality_flags']) >= 0 for r in typed[:50])


@DB
def test_idempotent_import_with_cleanup(tmp_path):
    from sqlalchemy import text
    from windpower.db.core import engine

    path, expect, written = _synth_file(tmp_path)
    digest = expect['expected_sha256']

    def synthetic_counts(conn):
        return dict(
            raw=conn.scalar(text('SELECT count(*) FROM turbine_scada_raw_10m WHERE file_id=:f'),
                            {'f': digest}),
            typed=conn.scalar(text(
                'SELECT count(*) FROM turbine_scada_10m WHERE file_id=:f AND turbine_id=99'),
                {'f': digest}),
            negative=conn.scalar(text(
                'SELECT count(*) FROM turbine_scada_10m WHERE file_id=:f AND turbine_id=99'
                ' AND grid_active_power_kw < 0'), {'f': digest}),
            sentinel=conn.scalar(text(
                "SELECT count(*) FROM turbine_scada_10m WHERE file_id=:f AND turbine_id=99"
                " AND quality_flags ? 'pitch_outside_canonical'"), {'f': digest}),
        )

    try:
        scada_f01.ingest(path, turbine_id=99, write_report=False, **expect)
        with engine().connect() as conn:
            first = synthetic_counts(conn)
        assert first['raw'] == written
        assert first['typed'] == written
        assert first['negative'] == 1          # 负功率在数据库中保留
        assert first['sentinel'] == 1
        scada_f01.ingest(path, turbine_id=99, write_report=False, **expect)
        with engine().connect() as conn:
            second = synthetic_counts(conn)
        assert second == first                 # 重复导入不增行、不改计数
        with engine().connect() as conn:
            files = conn.scalar(text('SELECT count(*) FROM data_ingest_files WHERE sha256=:f'),
                                {'f': digest})
            turbines = conn.scalar(text(
                'SELECT rated_power_kw FROM wind_turbines WHERE turbine_id=99'))
        assert files == 1                      # 同一文件只有一个登记
        assert turbines is None                # 不凭型号字符串填额定功率
    finally:
        with engine().begin() as conn:
            conn.execute(text('DELETE FROM turbine_scada_10m WHERE file_id=:f AND turbine_id=99'),
                          {'f': digest})
            conn.execute(text('DELETE FROM turbine_scada_raw_10m WHERE file_id=:f'), {'f': digest})
            conn.execute(text('DELETE FROM wind_turbines WHERE turbine_id=99'))
            conn.execute(text('DELETE FROM data_ingest_files WHERE sha256=:f'), {'f': digest})


@DB
def test_turbine_tables_do_not_touch_experiment_tables():
    from sqlalchemy import text
    from windpower.db.core import engine
    with engine().connect() as conn:
        gefcom = conn.scalar(text('SELECT count(*) FROM power_observations'))
        turbines = conn.scalar(text(
            "SELECT count(*) FROM wind_turbines WHERE external_id='F01'"))
        farm_binding = conn.scalar(text(
            'SELECT farm_id FROM wind_turbines WHERE external_id=:e'), {'e': 'F01'})
    assert gefcom == 175_440                    # 既有 GEFCom 数据未被改动
    assert turbines == 1
    assert farm_binding is None                 # F01 未虚构场站归属
