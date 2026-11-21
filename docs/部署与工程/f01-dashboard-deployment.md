---
title: F01 结果仪表盘 · 腾讯云部署说明
type: deployment
status: deployed-behind-security-group-port-80-pending
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
- **当前唯一阻塞：腾讯云安全组未放行 TCP:80**（实测 80/443/8080 均不可达，仅 22 放行；
  服务器本机访问 200 正常）。须在腾讯云控制台 → 云服务器 → 安全组 → 添加入站规则
  `TCP:80` 后公网可见。

## 展示内容与诚实边界

- 单机 10 分钟 SCADA 的研究性结果；广西政策准确率显示 `null（无评价资格）`，不给虚构分数。
- 测试集状态在页面上明确标注"本轮锁定"；可见性假设原文展示。
- 每次重新生成页面会带最新 git commit 与生成时间，防止旧结果被误读为当前状态。

## 服务器侧改动（2026-11-21 实测）

| 项 | 值 |
|---|---|
| Web 根 | `/var/www/windpower/`（ubuntu 属主，Caddy 以 www-data 读取） |
| Caddy 配置 | `/etc/caddy/Caddyfile`：`:80` 内 `handle /windpower/* { uri strip_prefix /windpower; root * /var/www/windpower; file_server }`，其余路径回落原占位站点；修改前已备份 `Caddyfile.bak.20261121` |
| 验证 | `caddy validate` 通过；`systemctl reload caddy`；本机 curl 200（100,320 字节） |
| 清理 | 临时 8080 探测进程已关闭，无残留 |

## 更新一页

```bash
# Omarchy：重新生成并推送（约 5 秒）
uv run python scripts/build_dashboard.py
rsync -vz artifacts/dashboard/index.html tencent:/var/www/windpower/index.html
```

## 安全与后续选项

1. **公网可见后无鉴权**：内容为聚合指标，风险可控；如需限制，
   Caddy 加 `basic_auth` 两行即可（需用户提供账号/密码哈希）。
2. **HTTPS/域名**：把域名 A 记录指到 49.233.91.122，Caddyfile 把 `:80` 换成域名，
   自动签 Let's Encrypt；需同时放行 443。
3. 服务器为 2 核 / 1.9 GB 小机型：纯静态页无压力；不要在上面跑训练或数据库。
