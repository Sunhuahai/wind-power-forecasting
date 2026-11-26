"""F01 十分钟 SCADA 的读取、审计与版本化入库。

流程：GB18030 原始 CSV → 完整数据审计（quality_scada 规则）→ data_ingest_files 登记
→ 原始行追加（turbine_scada_raw_10m，不可变）→ 规范行（turbine_scada_10m，含质量标记）。
同一文件重复导入幂等：file_id=sha256 为自然键，原始行 ON CONFLICT DO NOTHING，
规范行按同一 file_id 重新推导质量标记（DO UPDATE），均不产生重复记录。
原始文件不覆盖、不修改；本模块不把单机功率写入场站 operational_power。
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
from sqlalchemy.dialects.postgresql import insert

from windpower.data.quality_scada import (
    FIELD_MAP, parse_number, row_flags, rule_documentation, time_axis_report,
    column_profile,
)
from windpower.db.core import engine, upsert
from windpower.db.turbine_schema import (
    data_ingest_files, turbine_scada_10m, turbine_scada_raw_10m, wind_turbines,
)

EXPECTED_SHA256 = '1f81153d82242214201776d5cda6f302dc849d32f7f79f56cb3ec968f992b663'
EXPECTED_ROWS = 52_749
EXPECTED_COLUMNS = 54
TIME_SEMANTICS = 'source_naive_clock_unknown_timezone'
TIME_FORMAT = '%Y/%m/%d %H:%M'
INGESTER_VERSION = 'scada_f01_v1'
DEFAULT_CSV = Path('data/raw/f01/F01号风机十分钟数据.csv')
ARTIFACT_DIR = Path('artifacts/f01')
# 已知哨兵/监控通道（不进规范列，仅存原始载荷并做画像）。
MONITORED_ONLY = ('平均偏航集电环温度(℃)', '平均发电机进风口温度1(℃)', '平均发电机进风口温度2(℃)')


def csv_path() -> Path:
    override = os.getenv('F01_CSV_PATH')
    return Path(override) if override else DEFAULT_CSV


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_raw(path: Path | None = None, *, expected_sha256: str | None = None,
             expected_rows: int | None = None,
             expected_columns: int | None = None) -> pl.DataFrame:
    """全列按字符串读取（GB18030），保留 '--' 与空字段原样进入载荷。

    默认核对注册数据集（F01 正式版本）；测试可显式传入自己的期望值。
    """
    path = path or csv_path()
    if not path.exists():
        raise FileNotFoundError(
            f'F01 CSV not found at {path}; set F01_CSV_PATH or place the original file there')
    digest = sha256_of(path)
    expected_sha256 = expected_sha256 or EXPECTED_SHA256
    if digest != expected_sha256:
        raise ValueError(f'SHA-256 mismatch: {digest} != registered {expected_sha256}')
    frame = pl.read_csv(path, encoding='gb18030', infer_schema_length=0)
    expected_rows = EXPECTED_ROWS if expected_rows is None else expected_rows
    expected_columns = EXPECTED_COLUMNS if expected_columns is None else expected_columns
    if frame.height != expected_rows or frame.width != expected_columns:
        raise ValueError(
            f'Expected {expected_rows}x{expected_columns}, got {frame.height}x{frame.width}')
    return frame


def typed_rows(raw: pl.DataFrame, file_id: str = EXPECTED_SHA256, turbine_id: int = 1):
    """原始字符串 -> 规范数值行 + 行级质量标记；不修改任何原值。"""
    rows = []
    flags_counter: Counter = Counter()
    prev_time = None
    raw_dicts = raw.to_dicts()
    for row_no, r in enumerate(raw_dicts, start=1):
        time_text = str(r['统计时间'])
        observed_at = datetime.strptime(time_text, TIME_FORMAT)
        values = {}
        parse_issues = {}
        for src, dst in FIELD_MAP.items():
            if src == '统计时间':
                continue
            values[dst], issue = parse_number(r[src])
            if issue and issue not in ('empty',):
                parse_issues[dst] = issue
        for col in MONITORED_ONLY:
            values[f'_raw_{col}'], _ = parse_number(r[col])
        flags = row_flags(values, prev_time, observed_at)
        if parse_issues:
            flags['parse_issues'] = parse_issues
        for k in flags:
            flags_counter[k] += 1
        rows.append(dict(
            turbine_id=turbine_id, observed_at=observed_at, file_id=file_id,
            **{k: v for k, v in values.items() if not k.startswith('_raw_')},
            quality_flags=flags, raw_ref_row_no=row_no, raw_ref_file_id=file_id,
        ))
        prev_time = observed_at
    return rows, flags_counter


def audit(path: Path | None = None, *, expected_sha256: str | None = None,
          expected_rows: int | None = None,
          expected_columns: int | None = None) -> dict:
    """完整只读审计：文件级、时间轴、列级画像与规则命中；不写数据库。"""
    p = path or csv_path()
    raw = read_raw(p, expected_sha256=expected_sha256,
                   expected_rows=expected_rows, expected_columns=expected_columns)
    digest = expected_sha256 or EXPECTED_SHA256
    rows, flags_counter = typed_rows(raw, file_id=digest)
    return _audit_report(p, raw, digest, rows, flags_counter)


def _audit_report(p: Path, raw: pl.DataFrame, digest: str,
                  rows: list[dict], flags_counter: Counter) -> dict:
    times = [datetime.strptime(str(v), TIME_FORMAT) for v in raw['统计时间'].to_list()]
    time_report = time_axis_report(times)
    profiles = [column_profile(c, raw[c].to_list()) for c in raw.columns]

    grid = [r['grid_active_power_kw'] for r in rows]
    conv = [r['converter_active_power_kw'] for r in rows]
    neg = [v for v in grid if v is not None and v < 0]
    generating = [r['generating_h'] for r in rows]
    day_counts = Counter(t.date() for t in times)
    identity = {
        '设备名称': sorted(set(raw['设备名称'].to_list())),
        '型号名称': sorted(set(raw['型号名称'].to_list())),
        '馈线名称': sorted(set(raw['馈线名称'].to_list())),
    }
    winds = [r['nacelle_wind_speed_ms'] for r in rows]
    report = {
        'file': {
            'path': str(p), 'sha256': digest, 'byte_size': p.stat().st_size,
            'encoding': 'gb18030', 'rows': raw.height, 'columns': raw.width,
            'time_semantics': TIME_SEMANTICS,
            'interval_start_end_semantics': 'unconfirmed',
        },
        'identity': identity,
        'rated_power_note': (
            '型号名称 GW191/5270 仅暗示额定功率约 5270 kW，未经铭牌/合同证据确认；'
            '观察到的最大网侧有功功率 5314.22 kW 与之量级一致但不构成证明。'
            'rated_power_kw 保持 NULL，不得用于政策分母或归一化容量。'),
        'time_axis': time_report,
        'complete_days': sum(1 for _, c in day_counts.items() if c == 144),
        'incomplete_days': sum(1 for _, c in day_counts.items() if c != 144),
        'columns': profiles,
        'label_candidate': {
            'grid_active_power_kw': {
                'negative_count': len(neg), 'negative_share': len(neg) / len(grid),
                'negative_min_kw': min(neg) if neg else None,
                'negative_median_kw': sorted(neg)[len(neg) // 2] if neg else None,
                'zero_count': sum(1 for v in grid if v == 0),
                'max_kw': max(grid), 'min_kw': min(grid),
                'null_count': sum(1 for v in grid if v is None),
                'negative_with_generating_h_zero': sum(
                    1 for v, g in zip(grid, generating)
                    if v is not None and v < 0 and (g is None or g == 0)),
            },
            'converter_active_power_kw': {
                'negative_count': sum(1 for v in conv if v is not None and v < 0),
                'max_kw': max(v for v in conv if v is not None),
                'null_count': sum(1 for v in conv if v is None),
            },
        },
        'wind_speed': {
            'min_ms': min(winds), 'max_ms': max(winds),
            'median_ms': sorted(winds)[len(winds) // 2], 'null_count': sum(1 for v in winds if v is None),
        },
        'row_flag_hits': dict(flags_counter),
        'rules': rule_documentation(),
        'known_corrections': [
            '实测确认规划文档 §1.1 的时间轴结论：最大相邻时间戳差为 180 分钟'
            '（2024-05-27 14:40 → 17:40），另有一次 120 分钟缺口；'
            '"风机正常运行时间"最大 3 小时是状态时长列的独立事实，与时间缺口是两个概念。'],
        'availability_note': (
            '原文件不提供数据接收时间；离线回放可见性假设见 configs/f01_experiment.json，'
            '不得宣称为已验证的生产实时能力。'),
        'policy_eligibility': {
            'single_turbine_10m': True, 'farm_dispatch_reference': False,
            'GX_policy_accuracy': None,
            'ineligible_reason': 'single_turbine_scada_is_not_farm_grid_reference',
        },
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'ingester_version': INGESTER_VERSION,
    }
    return report


def write_quality_report(report: dict, out_dir: Path = ARTIFACT_DIR) -> Path:
    path = Path(out_dir) / 'quality_report.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                    encoding='utf-8')
    return path


def ingest(path: Path | None = None, turbine_id: int = 1,
           expected_sha256: str | None = None,
           expected_rows: int | None = None,
           expected_columns: int | None = None,
           write_report: bool = True, *, out_dir: Path | None = None) -> dict:
    """审计 + 版本化入库；重复导入幂等，不产生重复记录。"""
    p = path or csv_path()
    raw = read_raw(p, expected_sha256=expected_sha256,
                  expected_rows=expected_rows, expected_columns=expected_columns)
    digest = expected_sha256 or EXPECTED_SHA256
    typed, flags_counter = typed_rows(raw, file_id=digest, turbine_id=turbine_id)
    report = _audit_report(p, raw, digest, typed, flags_counter)
    now = datetime.now(timezone.utc)
    file_row = dict(
        file_id=digest, file_name=p.name, sha256=digest,
        byte_size=p.stat().st_size, encoding='gb18030',
        row_count=raw.height, column_count=raw.width,
        time_min=datetime.strptime(str(raw['统计时间'][0]), TIME_FORMAT),
        time_max=datetime.strptime(str(raw['统计时间'][-1]), TIME_FORMAT),
        time_semantics=TIME_SEMANTICS, ingested_at=now,
        ingester_version=INGESTER_VERSION,
        profile={k: report[k] for k in
                 ('time_axis', 'row_flag_hits', 'label_candidate', 'rated_power_note')},
        raw_path=str(p),
    )
    turbine_row = dict(
        turbine_id=turbine_id, farm_id=None, external_id=report['identity']['设备名称'][0],
        model_name=report['identity']['型号名称'][0], rated_power_kw=None,
        rated_power_evidence=report['rated_power_note'],
        feeder_name=report['identity']['馈线名称'][0], recorded_at=now,
        notes={
            'farm_binding': '场站归属未提供，farm_id 保持 NULL，不虚构场站',
            'single_turbine': True, 'source_file': p.name,
        },
        source_file_id=digest,
    )
    raw_payload_rows = []
    for row_no, r in enumerate(raw.to_dicts(), start=1):
        raw_payload_rows.append(dict(
            file_id=digest, row_no=row_no,
            raw_time_text=str(r['统计时间']),
            payload={k: (str(v) if v is not None else None) for k, v in r.items()},
            ingested_at=now,
        ))
    with engine().begin() as conn:
        upsert(conn, data_ingest_files, [file_row])
        upsert(conn, wind_turbines, [turbine_row])
        for start in range(0, len(raw_payload_rows), 500):
            stmt = insert(turbine_scada_raw_10m).values(raw_payload_rows[start:start + 500])
            conn.execute(stmt.on_conflict_do_nothing(
                index_elements=['file_id', 'row_no']))
        for start in range(0, len(typed), 500):
            stmt = insert(turbine_scada_10m).values(typed[start:start + 500])
            keys = ['turbine_id', 'observed_at', 'file_id']
            conn.execute(stmt.on_conflict_do_update(
                index_elements=keys,
                set_={c.name: stmt.excluded[c.name] for c in turbine_scada_10m.columns
                      if c.name not in keys}))
    counts = verify_counts()
    report['database'] = counts
    if write_report:
        write_quality_report(report, ARTIFACT_DIR if out_dir is None else out_dir)
    return report


def verify_counts() -> dict:
    """入库后行数核对：为幂等性与审计提供证据。"""
    from sqlalchemy import text
    with engine().connect() as conn:
        return dict(
            data_ingest_files=conn.scalar(text('SELECT count(*) FROM data_ingest_files')),
            wind_turbines=conn.scalar(text('SELECT count(*) FROM wind_turbines')),
            turbine_scada_raw_10m=conn.scalar(text('SELECT count(*) FROM turbine_scada_raw_10m')),
            turbine_scada_10m=conn.scalar(text('SELECT count(*) FROM turbine_scada_10m')),
            turbine_scada_10m_negative=conn.scalar(
                text('SELECT count(*) FROM turbine_scada_10m WHERE grid_active_power_kw < 0')),
            turbine_power_15m_proxy=conn.scalar(text('SELECT count(*) FROM turbine_power_15m_proxy')),
        )
