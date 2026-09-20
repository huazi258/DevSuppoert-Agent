# DevSupport Agent V2

DevSupport Agent V2 是供可信小型研发团队内部使用的、**只读**微服务故障调查 Agent。它从已配置的数据源和受范围约束的团队知识中收集证据，完成假设验证，并生成可追溯的调查结论、未确认事项和人工建议。

它不是可观测性平台、通用运维平台或自主修复系统。V2 不执行回滚、重启、发布、配置修改、Approval 或 Recovery Verification；`CONCLUDED` 仅表示调查结论已形成，不表示业务已经恢复。

## V2 文档

- [范围与安全边界](docs/V2_SCOPE.md)
- [产品设计](docs/V2_PRODUCT_DESIGN.md)
- [技术设计](docs/V2_TECH_DESIGN.md)
- [实施计划](docs/V2_IMPLEMENTATION_PLAN.md)
- [Docker Compose 部署与运行手册](docs/V2_DEPLOYMENT.md)

历史 V0/V1 文档和代码仅作为迁移与验证参考，不属于 V2 运行主流程。

## 本地启动

前置条件：Docker Desktop（含 Docker Compose）和 Node.js 22+（仅在本地运行 Web lint 时需要）。

```powershell
Copy-Item .env.example .env
# 编辑 .env：至少替换 PostgreSQL 密码；需要开始调查时配置 LLM、Embedding 和调查目标。
docker compose up -d --build
docker compose ps
```

默认仅监听 localhost：打开 `http://127.0.0.1:3000`。Compose 启动 Backend 前会执行 Alembic migration；更完整的配置、健康检查、备份恢复和可信内网部署说明见 [运行手册](docs/V2_DEPLOYMENT.md)。

> 无登录的 V2 实例只能部署在 localhost、可信内网或 VPN 后，不允许直接暴露公网。
