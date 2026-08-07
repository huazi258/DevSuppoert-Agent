# AGENTS.md

## Project

DevSupport Agent V0

面向研发团队的微服务故障调查与受控处置 Agent。

核心闭环：

```text
Incident
→ RAG
→ Hypothesis
→ Tool Investigation
→ Evidence Update
→ Resolution
→ Human Approval
→ Rollback
→ Recovery Verification
→ Report
```

V0 是可写入简历、可演示、可评测的正式版本，不是一次性 Demo。

---

## Project Documents

不要在 `AGENTS.md` 中推测完整需求或架构。

根据当前任务读取对应文档：

| 需要了解              | 文档                            |
| ----------------- | ----------------------------- |
| 产品目标、功能范围、V0 不做什么 | `docs/PRD.md`                 |
| 整体架构、模块边界、技术原则    | `docs/TECH_DESIGN.md`         |
| 当前开发阶段、任务顺序、验收标准  | `docs/IMPLEMENTATION_PLAN.md` |

开始实现前：

1. 阅读与当前任务直接相关的文档部分；
2. 检查仓库已有实现；
3. 确认当前任务边界和验证方式；
4. 再修改代码。

如果当前任务与上述文档冲突，停止扩大实现并报告冲突。

---

## Core V0 Constraints

长期必须遵守：

1. V0 使用单 Agent，不引入 Multi-Agent。
2. Agent 只能通过白名单 Tool 获取运行信息或执行操作。
3. 禁止给 Agent 任意 Shell、任意 SQL 或任意代码执行能力。
4. `rollback_deployment` 是 V0 唯一允许的副作用 Agent Action。
5. Rollback 必须经过代码层 Policy Gate 和真实 Human Approval。
6. Tool 执行成功不代表 Incident 已解决；必须独立执行 Recovery Verification。
7. 不得把 Eval 正确答案或 Fault 根因硬编码进 Prompt、Agent Workflow 或 Tool。
8. 不得擅自扩大 `docs/PRD.md` 定义的 V0 范围。

---

## Engineering Rules

* Python 业务边界优先使用类型标注和 Pydantic。
* Tool 必须有结构化 Input / Result。
* 外部调用必须有明确错误和超时处理。
* 数据库 Schema 变更通过 Migration。
* Secret 只通过环境变量配置，不提交 `.env`。
* 优先复用已有实现，不创建重复模块。
* 一次任务只修改当前目标需要的代码。
* 不因为“更企业级”自行引入新的大型基础设施。

---

## Verification

完成任务不能只检查代码。

必须运行与当前修改相关的：

* tests；
* lint / type checks（如果仓库已配置）；
* 必要的 API / Docker / Workflow 实际验证。

然后检查：

```bash
git diff
git status
```

不得通过删除测试、降低断言或 hardcode success 来隐藏失败。

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

每完成一个 `docs/IMPLEMENTATION_PLAN.md` 中定义的子任务，并通过相关测试和验收后，必须创建一次独立 Git commit。

提交前必须：

1. 运行当前任务相关测试和验证；
2. 执行 `git diff --check`；
3. 检查 `git status --short` 和 `git diff --stat`；
4. 确认没有 Secret、缓存、生成垃圾或范围外修改。

如果子任务因为阻塞没有完成，不创建“完成”commit。

完成 commit 后必须在任务报告中提供：

- commit hash；
- commit message。

## Git Workflow

开发默认直接基于 `main`。

每完成一个 `docs/IMPLEMENTATION_PLAN.md` 中的子任务，并通过测试和验收后：

1. 检查 diff；
2. 创建独立 commit；
3. push 到 `origin/main`；
4. 停止，不自动开始下一子任务。

除非用户明确要求，否则：

- 不创建 feature branch；
- 不创建 Pull Request；
- 不执行 force push；
- 不修改或重写已有公共 commit 历史。

如果 `origin/main` 在开发期间出现新的远程提交，应先安全同步并解决问题，再 push。
