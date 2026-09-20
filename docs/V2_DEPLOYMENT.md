# DevSupport Agent V2 部署与运行手册

本文覆盖 V2 的 Docker Compose 单机和可信内网部署。部署包含 PostgreSQL + pgvector、FastAPI Backend 与 Next.js Web；不部署 Fault Lab、可观测性平台或任何具有副作用的运行能力。

> **安全边界：无登录的 V2 实例只能部署在 localhost、可信内网或 VPN 后，不允许直接暴露公网。** 默认 Compose 端口只绑定 `127.0.0.1`。若供内网使用，部署方必须把 Web 端口绑定到受信任的内网/VPN 接口，并自行保证网络边界；不要将其绑定到公网 IP，也不要使用公网端口映射。

## 前置条件

- Docker Engine / Docker Desktop，且包含 Docker Compose v2；
- 部署主机能访问所配置的 LLM、Embedding 与已批准的只读 Provider；
- 至少有一个用于 PostgreSQL 数据卷备份的受保护位置；
- 不将 `.env`、备份文件或 Provider 凭据加入 Git、日志或工单附件。

首次部署可确认 Compose 文件已能被解析：

```powershell
docker compose --env-file .env.example config
```

## 配置 `.env`

从模板开始，并只授权受信任的部署人员读取该文件：

```powershell
Copy-Item .env.example .env
```

至少替换以下两处相同的 PostgreSQL 密码：

- `DEVSUPPORT_POSTGRES_PASSWORD`
- `DEVSUPPORT_DATABASE_URL` 中 `postgres` 主机名前的密码部分

`DEVSUPPORT_DATABASE_URL` 的默认主机名是 `postgres`，即 Compose 内部服务名；不要把它改为宿主机地址。PostgreSQL 未发布宿主机端口，Backend 通过 Compose 网络访问它。

主要变量如下。Backend 同时兼容无前缀的 `LLM_*` / `EMBEDDING_*` 和对应的 `DEVSUPPORT_*` 名称；建议在部署文件中保持模板的写法。

| 变量 | 用途 |
| --- | --- |
| `DEVSUPPORT_DATABASE_URL` | Backend 到 Compose 内部 PostgreSQL 的 SQLAlchemy URL。 |
| `DEVSUPPORT_POSTGRES_DB` / `DEVSUPPORT_POSTGRES_USER` / `DEVSUPPORT_POSTGRES_PASSWORD` | 初始化数据库和数据卷的凭据。密码必须是部署专用的强密码。 |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | OpenAI-compatible LLM 配置。 |
| `EMBEDDING_MODEL` / `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | OpenAI-compatible Embedding 配置。 |
| `DEVSUPPORT_INVESTIGATION_TARGET_CONFIGS` | JSON 数组：已批准的调查目标、服务白名单及能力选择。 |
| `DEVSUPPORT_PROVIDER_CONFIGS` | JSON 数组：目标能力到 opaque provider reference 的映射。 |
| `DEVSUPPORT_PROVIDER_BACKEND_CONFIGS` | JSON 数组：Provider endpoint 和 credential 等仅后端可见的配置。 |
| `DEVSUPPORT_OPENSEARCH_URL` / `DEVSUPPORT_PROMETHEUS_URL` | 兼容的全局 Provider 地址；新配置优先使用 Provider backend config。 |
| `DEVSUPPORT_FAULT_LAB_ORDER_SERVICE_URL` / `DEVSUPPORT_FAULT_LAB_PAYMENT_SERVICE_URL` | 仅本地 Fault Lab 兼容用途；不是内网生产调查目标的默认配置。 |
| `DEVSUPPORT_BACKEND_API_URL` | Next.js 在 Compose 网络中代理 `/api` 时使用的 Backend 地址，默认 `http://backend:8000`。 |
| `NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL` | 浏览器 API 前缀，Compose 保持 `/api`，避免浏览器直接解析内部 `backend` 主机名。 |
| `DEVSUPPORT_WEB_BIND_ADDRESS` / `DEVSUPPORT_WEB_PORT` | Web 对宿主机暴露的地址和端口，默认 `127.0.0.1:3000`。 |
| `DEVSUPPORT_BACKEND_BIND_ADDRESS` / `DEVSUPPORT_BACKEND_PORT` | Backend 诊断端口，默认仅 `127.0.0.1:8002`。普通使用经 Web 的 `/api` 代理。 |

调查目标配置仅引用不透明的 provider key，不能包含 URL 或 Secret。URL 和 Secret 只能放在 `DEVSUPPORT_PROVIDER_BACKEND_CONFIGS` 等 Backend 环境变量中，绝不放入目标配置、浏览器响应、Prompt、数据库普通字段或 Git。

示意（替换 UUID、服务名、地址和凭据；不得提交真实值）：

```text
DEVSUPPORT_INVESTIGATION_TARGET_CONFIGS=[{"target_id":"00000000-0000-0000-0000-000000000001","slug":"orders-prod","display_name":"订单系统生产环境","description":"订单系统的受控生产调查目标。","environment":"production","services":[{"name":"orders-api","display_name":"订单 API"}],"logs":{"enabled":true,"adapter_type":"opensearch","provider_config_ref":"orders-logs"},"metrics":{"enabled":true,"adapter_type":"prometheus","provider_config_ref":"orders-metrics"}}]
DEVSUPPORT_PROVIDER_CONFIGS=[{"provider_config_ref":"orders-logs","adapter_type":"opensearch","backend_config_key":"orders-logs-backend"},{"provider_config_ref":"orders-metrics","adapter_type":"prometheus","backend_config_key":"orders-metrics-backend"}]
DEVSUPPORT_PROVIDER_BACKEND_CONFIGS=[{"backend_config_key":"orders-logs-backend","adapter_type":"opensearch","endpoint":"https://logs.internal.example","credential":"replace-me"},{"backend_config_key":"orders-metrics-backend","adapter_type":"prometheus","endpoint":"https://metrics.internal.example","credential":"replace-me"}]
```

每个启用能力的 `provider_config_ref`、`adapter_type` 与 backend config 必须一致。Provider `credential` 是 Secret，示意中的值不可使用；配置前请按 [M2 目标配置契约](../apps/backend/src/devsupport_backend/target_config.py) 校验。

## Build、启动与 Migration

首次或镜像更新后：

```powershell
docker compose up -d --build
docker compose ps
```

Backend 等待 PostgreSQL `healthy` 后执行 `alembic upgrade head`，随后运行幂等的 V2 bootstrap，再启动 API。bootstrap 仅将 `DEVSUPPORT_INVESTIGATION_TARGET_CONFIGS` 中的非 Secret Target / Service 标识和显示元数据写入数据库；不会创建 Incident、Round、Evidence、Report、知识文档或 Provider 凭据。每次启动可安全重跑，已存在的匹配 Target / Service 不会重复创建。

因此，`docker compose down -v` 后只需确认 `.env` 仍保留正确的部署配置，再重新启动 Compose；不要用手工 SQL 恢复可选择的 Target / Service。也可以在维护窗口显式执行 migration 和 bootstrap：

```powershell
docker compose up -d postgres
docker compose run --rm --no-deps backend alembic upgrade head
docker compose run --rm --no-deps backend python -m devsupport_backend.bootstrap
```

首次访问 `http://127.0.0.1:3000`。只有配置并持久化的 Investigation Target 才会出现在创建 Incident 的目标列表中。未配置 LLM/Embedding/Provider 时，健康检查仍可通过，但不要期待调查能够完成；不得因外部 Provider 不可用而声称调查成功。bootstrap 不会伪造 Embedding、LLM 或运行数据：Markdown 上传需要可访问的 Embedding Provider，完整调查还需要相应的只读 Provider 和 LLM 配置。

## 健康检查、日志和日常操作

```powershell
# 查看依赖顺序和健康状态
docker compose ps

# Backend liveness（默认只绑定 localhost）
Invoke-RestMethod http://127.0.0.1:8002/health

# Web 可用性
Invoke-WebRequest http://127.0.0.1:3000/ -UseBasicParsing

# 追踪所有服务，或单独查看 Backend
docker compose logs -f
docker compose logs -f backend

# 停止（保留 PostgreSQL 数据卷）和重新启动
docker compose stop
docker compose start

# 移除容器与网络，但保留数据卷
docker compose down
```

`postgres_data` 是命名数据卷，`docker compose down` 不会删除它。除非确认不再需要所有调查历史、Evidence、报告和知识索引，否则不要运行 `docker compose down -v`。

## PostgreSQL 备份与恢复

在升级、迁移或更换 Secret 前先备份。自定义格式适合 `pg_restore`：

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > .\backups\devsupport-$(Get-Date -Format yyyyMMdd-HHmmss).dump
```

恢复会覆盖目标库中的同名对象。先停止写入方，并确认备份来源正确：

```powershell
docker compose stop backend web
$postgresId = docker compose ps -q postgres
docker cp .\backups\devsupport-YYYYMMDD-HHMMSS.dump "${postgresId}:/tmp/devsupport.dump"
docker compose exec postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/devsupport.dump'
docker compose start backend web
```

对于纯 SQL 备份，将文件复制到容器后使用 `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/backup.sql`。恢复后检查 `docker compose ps`、Backend `/health`，并按需执行 `docker compose exec backend alembic current`；如果备份来自旧 Schema，执行 `docker compose exec backend alembic upgrade head`。

## 更新 Secret

1. 在受保护的 `.env` 或等价 Secret 注入机制中更新 LLM、Embedding 或 Provider 凭据；不要修改 Git 中的模板。
2. 对 LLM/Embedding/Provider Secret，重建或强制重建 Backend：

   ```powershell
   docker compose up -d --force-recreate backend
   ```

3. `DEVSUPPORT_BACKEND_API_URL` 或 `NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL` 改变时，Web rewrite 在构建期生成，执行：

   ```powershell
   docker compose up -d --build --force-recreate web
   ```

4. 修改 `DEVSUPPORT_POSTGRES_PASSWORD` 不是仅重建容器即可完成：已有数据卷中的 PostgreSQL 角色密码也必须在维护窗口内同步更新。先完成数据库角色密码变更、更新 `DEVSUPPORT_DATABASE_URL`，再重建 PostgreSQL 和 Backend，并验证连接。首次部署前设置强密码优于事后轮换。

所有修改后运行 `docker compose ps` 并检查 `/health`。不要在 `docker compose config`、日志截图或 shell history 中传播 Secret。
