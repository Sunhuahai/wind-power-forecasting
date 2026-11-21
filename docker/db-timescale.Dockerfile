# 开发/部署数据库镜像：PostgreSQL 18.6 + TimescaleDB 2.32.1。
# 基础镜像保持项目锁定的 postgres:18.6（health() 校验 18.6），只追加
# 官方 apt 仓库中针对 PG 18.6 构建的 timescaledb-2-postgresql-18。
# 版本钉死，重建可复现；升级须按 docs/部署与工程/ 两份激活脚本的预检流程重新核对。
FROM postgres:18.6

# 两个版本必须成对钉死：timescaledb 的 -1806 构建对应 PG 18.6。
# 不钉住 postgresql-18 时，apt 会把服务器升到最新 18.x 补丁版，破坏 health() 的 18.6 校验。
ARG POSTGRES_PACKAGE_VERSION=18.6-1.pgdg13+2
ARG TIMESCALEDB_VERSION=2.32.1~debian13-1806

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/timescaledb-archive-keyring.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/timescaledb-archive-keyring.gpg] https://packagecloud.io/timescale/timescaledb/debian/ trixie main" \
      > /etc/apt/sources.list.d/timescaledb.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      "postgresql-18=${POSTGRES_PACKAGE_VERSION}" \
      "timescaledb-2-loader-postgresql-18=${TIMESCALEDB_VERSION}" \
      "timescaledb-2-postgresql-18=${TIMESCALEDB_VERSION}" \
 && apt-get purge -y curl gnupg \
 && apt-get autoremove -y \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*
