# GEFCom2014 研究线（已归档）

> **状态：已归档，2026-11-23 起不再作为项目主线。**
> 后续研究统一使用 F01 广西风机十分钟 SCADA 数据，见
> [F01 数据审计](../../研究记录/f01-data-audit.md) ·
> [F01 首轮基线](../../研究记录/f01-baseline-first-round.md) ·
> [F01 评价协议](../../研究记录/f01-evaluation-protocol.md)。

## 为什么归档

项目主线已从"公开竞赛数据算法研究"转为"真实广西风机数据"。GEFCom2014-Wind 是
小时级、10 个匿名站、归一化功率的竞赛数据，没有 MW 容量、没有 NWP 起报/接收时间、
没有场站运行状态，能支撑算法对比，但无法支撑真实业务评价。保留它只会让两条评价口径混在一起。

## 归档范围

| 类别 | 位置 | 说明 |
|---|---|---|
| 原始数据 | `data/archive/gefcom2014/`（本地，不入 git） | 官方 ZIP、Task15/TaskExpVars15 十个 zone、答案文件，约 216 MB |
| 实验与优化记录 | 本目录 `*.md` / `optimization-progress.json` | 十站基线、kpower 复现、9 节点优化链、饱和评估、论文与图表 |
| 图表 | `本目录/figures/` | 优化进展曲线、分站误差矩阵、预测曲线等 |

## 仍然保留、可以复现

按归档时的决定，**代码和数据库旧表不删除**：

- `src/windpower/` 的 ingest、模型、CLI 旧命令保持原样；
- 数据库 `wind_farms`（10 个 GEFCom zone）、`power_observations`、`nwp_forecasts`、
  `predictions` 等旧实验表保留，`tests/test_scada_f01.py` 中"GEFCom 数据未被改动"的断言继续有效；
- 需要复跑旧结果时，把 `data/archive/gefcom2014/` 的文件放回 `data/raw/` 即可，命令不变。

## 引用注意

- 本目录文档及图表中的链接可能指向归档前的路径，已失效时不代表结论失效；
- 任何 GEFCom 指标（例如合并 RMSE 0.156010）**不得**与 F01 指标出现在同一张排行榜里，
  两者的数据、单位、分辨率、评价口径都不同；
- GEFCom 结果不能再用于支撑广西考核结论。
