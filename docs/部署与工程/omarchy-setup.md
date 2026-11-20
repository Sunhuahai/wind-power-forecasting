# Omarchy 环境搭建与迁移步骤

更新：2026-11-20。背景：项目不再在 Mac 上运行，整体迁到 Omarchy（`ssh omarchy`，venti@59.66.19.164），仓库放 `~/Projects/wind-power-forecasting`。

## 一、为什么迁

- 生产目标一直是 Linux（AGENTS.md 规则）；Mac 上没有 Docker，数据库和推理链根本跑不起来。
- iCloud 路径含空格和波浪号，远端脚本引用容易踩坑；Linux 本地路径干净。

## 二、迁移步骤（2026-11-20 执行完成）

1. Mac 上 `git push origin main`，保证最新提交进 GitHub。✅
2. Omarchy 上：`git clone https://github.com/Sunhuahai/wind-power-forecasting.git ~/Projects/wind-power-forecasting`。✅
3. rsync 同步 git 不管的大文件（约 572MB：模型 joblib、冻结预测 CSV）。✅
4. Omarchy 上装依赖并起库（`uv sync --extra dev` → `db-start` → `db-init`）。✅
5. 导入十站数据（175,440 行功率 + 87,720 行 NWP）。✅
6. 验证：`uv run pytest` 42 通过；`WINDPOWER_TEST_DB=1 uv run pytest` 44 全过。✅
7. P2 回放一致性：`scripts/verify_serving_replay.py` 通过——42432 行与冻结链逐行最大偏差 1.9e-14，合并 RMSE 0.1560098484603837 完全一致。✅

**执行中发现的两个坑（下次重建环境要记住）：**

- **Docker Hub 直连超时**：`registry-1.docker.io` 在 Omarchy 上不可达。用 daocloud 镜像拉取再打标签：
  ```sh
  docker pull docker.m.daocloud.io/library/postgres:18.6
  docker tag docker.m.daocloud.io/library/postgres:18.6 postgres:18.6
  ```
- **Dropbox 不可达**：`windpower download` 在 Omarchy 上失败，原始数据从 Mac rsync 过去（`data/raw/`，215MB）。

## 三、Omarchy 环境现状（2026-11-20 检查）

- Arch 内核 7.2.15-1-omarchy；git、docker、python3 系统自带，uv 走 mise shim。
- `~/Projects` 已有多个项目（GeVant4、博士论文等），风电项目并列放入即可。

## 四、后续口径

- 数据库、训练、推理、测试一律在 Omarchy 跑；Mac 只作编辑与跳板。
- Mac 上的 iCloud 仓库目录在迁移验证通过后处置（删除前先确认 GitHub 与 Omarchy 两边都完整）。
