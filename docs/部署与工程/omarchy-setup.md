# Omarchy 环境搭建与迁移步骤

更新：2026-11-20。背景：项目不再在 Mac 上运行，整体迁到 Omarchy（`ssh omarchy`，venti@59.66.19.164），仓库放 `~/Projects/wind-power-forecasting`。

## 一、为什么迁

- 生产目标一直是 Linux（AGENTS.md 规则）；Mac 上没有 Docker，数据库和推理链根本跑不起来。
- iCloud 路径含空格和波浪号，远端脚本引用容易踩坑；Linux 本地路径干净。

## 二、迁移步骤（2026-11-20 执行）

1. Mac 上 `git push origin main`，保证最新提交进 GitHub。
2. Omarchy 上：`git clone https://github.com/Sunhuahai/wind-power-forecasting.git ~/Projects/wind-power-forecasting`。
3. 同步 git 不管的大文件（模型 joblib、冻结预测 CSV、.env）：
   ```sh
   rsync -a --exclude .git --exclude .venv --exclude __pycache__ \
     --exclude .pytest_cache --exclude .DS_Store --exclude 'data/raw' \
     "<Mac 仓库目录>/" omarchy:Projects/wind-power-forecasting/
   ```
   `data/raw` 不同步：Omarchy 用 `uv run windpower download` 从公开 Dropbox 链接直接下载。
4. Omarchy 上装依赖并起库：
   ```sh
   cd ~/Projects/wind-power-forecasting
   uv sync --extra dev
   uv run windpower db-start && uv run windpower db-init
   uv run windpower download
   for s in $(seq 1 10); do uv run windpower import-data --site $s; done
   ```
5. 验证：`uv run pytest`（不含库）；`WINDPOWER_TEST_DB=1 uv run pytest`（含库）。
6. P2 回放一致性：`uv run python scripts/verify_serving_replay.py`，
   通过标准 = 与冻结的 mlp_seedavg_blend 逐行最大偏差 ≤ 1e-9，合并 RMSE ≈ 0.156010。

## 三、Omarchy 环境现状（2026-11-20 检查）

- Arch 内核 7.2.15-1-omarchy；git、docker、python3 系统自带，uv 走 mise shim。
- `~/Projects` 已有多个项目（GeVant4、博士论文等），风电项目并列放入即可。

## 四、后续口径

- 数据库、训练、推理、测试一律在 Omarchy 跑；Mac 只作编辑与跳板。
- Mac 上的 iCloud 仓库目录在迁移验证通过后处置（删除前先确认 GitHub 与 Omarchy 两边都完整）。
