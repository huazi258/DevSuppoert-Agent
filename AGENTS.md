# AGENTS.md

## Project

DevSupport Agent V2 是供可信小型研发团队内部使用的、只读的微服务故障调查 Agent。

核心闭环：

```text
Incident
→ 选择已配置的调查目标与服务
→ RAG + Runtime Evidence
→ Hypothesis
→ Tool Investigation
→ Evidence Update
→ Conclusion / Inconclusive / Failure
→ Versioned Report
```

终态用户可补充新的观察信息，启动同一 Incident 的下一调查轮次。新轮次保留旧 Evidence、结论和报告，但不改写历史。

V2 不执行或记录人工修复，不包含审批、回滚或恢复验证。`CONCLUDED` 只表示调查结论已形成，不表示业务系统已经恢复。

当前目标是在可接受较大重写的前提下，交付一个：

* stable；
* explainable；
* evaluable；
* portable across supported investigation environments；
* 面向非 Workflow 专家的中文内部调查工具。

---

## Project Documents

不要在 `AGENTS.md` 中推测完整需求或架构。

根据当前任务读取对应文档：

| 需要了解 | 文档 |
| --- | --- |
| V2 产品定位、功能范围、非目标、业务对象与状态 | `docs/V2_SCOPE.md` |
| V2 用户流程、中文 UI 和交互边界 | `docs/V2_PRODUCT_DESIGN.md` |
| V2 Tool、Adapter、RAG、可靠性和部署设计 | `docs/V2_TECH_DESIGN.md` |
| 当前开发阶段、任务顺序、验收标准 | `docs/V2_IMPLEMENTATION_PLAN.md` |

开始实现前：

1. 阅读与当前任务直接相关的 V2 文档部分；
2. 检查仓库已有实现和可复用测试；
3. 确认当前任务边界和验证方式；
4. 再修改代码。

`docs/PRD.md`、`docs/TECH_DESIGN.md`、`docs/IMPLEMENTATION_PLAN.md`、`docs/V1_*.md` 和 V0/V1 Release 文档保留为历史实现与验证参考。V2 开发任务若与 V2 文档冲突，停止扩大实现并报告冲突。

---

## Core Constraints

长期必须遵守：

1. V2 使用单 Agent，不引入 Multi-Agent。
2. Agent 只能通过白名单、结构化、只读 Tool 获取运行信息。
3. 禁止给 Agent 任意 Shell、任意 SQL、任意 HTTP、任意 Token 或任意代码执行能力。
4. V2 Agent Runtime 不得包含回滚、重启、发布、配置修改或其他副作用 Action；不得暴露 V1 的 Approval、Rollback、Action Execution 或 Recovery Verification 路径。
5. Tool 执行成功不代表结论成立；关键结论必须绑定 Runtime Evidence 或知识 Citation。没有足够证据时必须终止为 `INCONCLUSIVE`，不得伪造结论或声称系统已恢复。
6. LLM 只负责受 Schema 约束的假设、计划、证据解释和结论草稿。确定性代码负责 Tool 校验、范围、预算、重试、状态转移、持久化和报告绑定。
7. 不得把 Eval 正确答案或 Fault 根因硬编码进 Prompt、Agent Workflow 或 Tool。
8. Fault Lab 是确定性 Eval / Ground Truth 环境；OpenTelemetry Demo 用于证明 Adapter 泛化。V2 不负责搭建或运营可观测性平台。
9. 调查目标的 Provider 地址、Secret、服务映射与可用能力由部署配置管理。普通用户和 Agent 不得自助提交任意数据源连接。
10. 知识检索必须按调查目标、服务、环境和文档状态隔离；范围过滤必须在关键词和向量检索两侧执行。
11. V2 不做公网 SaaS、多租户、SSO、账号体系、RBAC、自建可观测性平台、复杂文档/OCR、Kubernetes/数据库/代码 Agent 或大型 Provider 矩阵。
12. V2 面向可信内网或 VPN 环境。无认证实例不得直接暴露到公网。
13. 不得擅自扩大 `docs/V2_SCOPE.md` 定义的 V2 范围。

---

## Engineering Rules

* Python 业务边界优先使用类型标注和 Pydantic。
* Tool 必须有结构化 Input / Result；所有外部调用必须有明确错误、超时与有限重试处理。
* Adapter 是受审查的代码；目标地址、服务映射和 Secret 是部署配置。不得把 Secret 写入 Prompt、浏览器响应、普通数据库字段、日志或 Git。
* 数据库 Schema 变更通过 Migration。
* 调查轮次、Evidence、结论和报告必须可追溯；新轮次不得覆盖旧轮次历史。
* 知识上传初期仅支持 Markdown。摄取失败不得部分启用文档；只有已启用且范围匹配的文档可检索。
* 用户产品文案、V2 文档与报告使用中文；代码标识、API 字段和数据库迁移名保持英文。
* 优先复用已有实现，不创建重复模块；允许为符合 V2 设计而重构或替换 V1 路径。
* 一次任务只修改当前目标需要的代码。
* 不因为“更企业级”自行引入新的大型基础设施。

---

## Verification

完成任务不能只检查代码。

必须运行与当前修改相关的：

* tests；
* lint / type checks（如果仓库已配置）；
* 必要的 API / Docker / Workflow 实际验证；
* 对 RAG、Adapter、状态机或 UI 改动，运行相应的隔离、错误边界、调查轮次或浏览器验证。

然后检查：

```bash
git diff
git status
```

不得通过删除测试、降低断言、隐藏错误或 hardcode success 来隐藏失败。

---

## Completion Report

每个任务完成后汇报：

```text
Implemented
Tests
Manual Verification
Files Changed
Known Issues
Scope Check
Ready for Review
```

完成当前任务后停止，不自动进入下一开发阶段。

## Git Commit Rule

每完成一个 `docs/V2_IMPLEMENTATION_PLAN.md` 中定义的子任务，并通过相关测试和验收后，必须创建一次独立 Git commit。

提交前必须：

1. 运行当前任务相关测试和验证；
2. 执行 `git diff --check`；
3. 检查 `git status --short` 和 `git diff --stat`；
4. 确认没有 Secret、缓存、生成垃圾或范围外修改。

如果子任务因为阻塞没有完成，不创建“完成”commit。

完成 commit 后必须在任务报告中提供：

* commit hash；
* commit message。

## Git Workflow

开发默认直接基于 `main`。

每完成一个 `docs/V2_IMPLEMENTATION_PLAN.md` 中的子任务，并通过测试和验收后：

1. 检查 diff；
2. 创建独立 commit；
3. push 到 `origin/main`；
4. 停止，不自动开始下一子任务。

除非用户明确要求，否则：

* 不创建 feature branch；
* 不创建 Pull Request；
* 不执行 force push；
* 不修改或重写已有公共 commit 历史。

如果 `origin/main` 在开发期间出现新的远程提交，应先安全同步并解决问题，再 push。
