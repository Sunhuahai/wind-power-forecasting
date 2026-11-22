---
title: F01 结果仪表盘 · 腾讯云部署说明
type: deployment
status: deployed-public-basic-auth-active
created: 2026-11-21
week: 2026-W47
server: tencent（49.233.91.122，Ubuntu 24.04，Caddy）
tags: [风电功率预测, F01, 可视化, 部署]
---

# F01 结果仪表盘 · 腾讯云部署说明

## 一页结论

- 生成：`uv run python scripts/build_dashboard.py` → `artifacts/dashboard/index.html`
  （单文件、零外部依赖、内联 SVG 图表，国内网络无需 CDN）。
- 内容：只含**聚合指标**（验证期 MAE/RMSE/Bias、分提前量、逐日、分组诊断、
  日级功率包络 367 行、审计规则命中、政策资格与门禁状态），无逐行原始 SCADA，
  符合"报告只公开聚合结果"的数据政策。
- 部署：腾讯云 CVM `49.233.91.122`，Caddy 静态站点，路径
  **`http://49.233.91.122/windpower/`**；原占位站点未动。
- 2026-11-21 已放行 TCP:80（用户在腾讯云控制台操作）：Omarchy 实测公网 200（~0.02 s）。
- **basic_auth 已启用**：账号 `f01`，密码由用户设置并自行保管，**不入公开仓库**
  （服务器 Caddyfile 只存 bcrypt 哈希；占位站点不设密码，仅保护 /windpower/*）。
- 注意：浏览器可能把裸地址自动升级为 https://（443 未开）→ 必须手动输入 `http://`。

## 展示内容与诚实边界

- 按用户要求（11-21）页面收敛为**纯结果视图**：只展示训练/测试指标与图表，
  评估协议、政策资格、诚实声明区块已移除；这些纪律信息保留在仓库证据文件
  （`artifacts/f01/summary.json`、`report.md`）中，页面不代表政策评价结论。
- 测试集已于 11-22 一次性评分，页脚标注"已一次性评分"；页头带生成时间与代码提交，
  防止旧结果被误读为当前状态。
- 已知并修复的渲染缺陷：折线颜色键名 bug（`colors`/`color` 不一致导致全浏览器折线不可见），
  修复后经渲染 DOM 属性审计 + 分区像素验证 + 独立代读确认。

## 服务器侧改动（2026-11-21 实测）

| 项 | 值 |
|---|---|
| Web 根 | `/var/www/windpower/`（ubuntu 属主，Caddy 以 www-data 读取） |
| Caddy 配置 | `/etc/caddy/Caddyfile`：`:80` 内 `handle /windpower/* { uri strip_prefix /windpower; root * /var/www/windpower; file_server }`，其余路径回落原占位站点；修改前已备份 `Caddyfile.bak.20261121` |
| 验证 | `caddy validate` 通过；reload 后实测：无凭据 401、错误凭据 401、`f01` + 用户密码 200（106 KB）；`Cache-Control: no-cache` |
| 清理 | 临时 8080 探测进程已关闭，无残留 |

## 更新一页

```bash
# Omarchy：重新生成并推送（约 5 秒）
uv run python scripts/build_dashboard.py
rsync -vz artifacts/dashboard/index.html tencent:/var/www/windpower/index.html
```

## 安全与后续选项

1. ~~公网可见后无鉴权~~ 已启用 basic_auth（账号 `f01`）。更换密码：
   `caddy hash-password --plaintext '新密码'` 后更新 Caddyfile 并 reload。
2. **HTTPS/域名**：把域名 A 记录指到 49.233.91.122，Caddyfile 把 `:80` 换成域名，
   自动签 Let's Encrypt；需同时放行 443。
3. 服务器为 2 核 / 1.9 GB 小机型：纯静态页无压力；不要在上面跑训练或数据库。
