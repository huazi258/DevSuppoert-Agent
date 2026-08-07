# DevSupport Agent V0 PRD

> Product Requirements Document
> Status: Draft for Scope Freeze
> Version: V0
> Target: 首个可写入简历、可演示、可评测的正式版本

---

## 1. 项目定位

### 1.1 一句话定位

DevSupport Agent 是一个面向研发团队的 **微服务故障调查与受控处置 Agent**。

研发人员人工提交故障任务后，系统综合内部技术知识与当前运行环境证据，形成并验证故障假设；必要时提出回滚方案，经人工批准后执行，并重新验证系统是否恢复，最终生成结构化调查报告。

### 1.2 核心业务链路

```text
人工提交故障
→ 信息结构化
→ 检索相关知识
→ 形成故障假设
→ 调用调查工具收集证据
→ 更新 / 淘汰假设
→ 形成故障结论与处置建议
→ 人工审批
→ 受控执行
→ 恢复验证
→ 生成调查报告
```

### 1.3 项目不是什么

DevSupport Agent V0 不是：

* 普通 RAG 聊天机器人；
* 用户粘贴一段日志后由 LLM 猜测根因的日志分析工具；
* 全自动生产环境 SRE Agent；
* 完整 ITSM / Jira 替代品；
* 通用多 Agent 平台；
* 自动代码修复系统。

V0 的核心价值是：

> 让 Agent 基于真实证据完成一次可追踪、可控制、可验证的故障调查闭环。

---

# 2. 项目目标

V0 首要目标不是覆盖大量故障，而是证明一条完整 Agent 业务链路真实成立。

完成后应能够展示以下能力：

1. Python / FastAPI 后端工程；
2. RAG 知识检索；
3. Agent Tool Calling；
4. 多步骤有状态工作流；
5. 故障假设与证据管理；
6. Human-in-the-loop；
7. 有副作用操作的风险控制；
8. 工作流暂停与恢复；
9. 处置后的恢复验证；
10. Agent Trace 与工具调用记录；
11. 固定任务 Eval；
12. Docker 化运行；
13. 可向面试官解释的真实业务价值。

---

# 3. 目标用户

V0 主要面向：

### 3.1 研发工程师

典型需求：

* 新版本发布后接口异常；
* 服务调用失败；
* 自己负责的服务出现大量错误；
* 希望快速找到相关日志、Trace 和发布记录。

### 3.2 测试 / 研发支持人员

典型需求：

* staging 环境发布后出现异常；
* 测试接口大量失败；
* 需要快速判断问题属于当前服务还是下游服务。

### 3.3 值班 / SRE 人员

V0 不试图替代 SRE，而是辅助完成：

* 初步信息整理；
* 证据收集；
* 历史问题检索；
* 故障假设验证；
* 标准处置建议。

---

# 4. V0 运行环境边界

V0 只操作：

```text
local / staging
```

不执行真实生产环境变更。

有副作用的操作只允许作用于项目自身的故障实验环境。

---

# 5. 用户输入

## 5.1 V0 任务入口

V0 只支持：

> **研发人员人工创建故障任务。**

暂不支持监控告警、GitHub Webhook、Jira 等自动任务入口。

## 5.2 用户至少提供

创建故障时包含：

* 问题描述；
* 目标服务；
* 环境；
* 大致时间范围。

示例：

```text
服务：order-service
环境：staging
时间：14:00 后

问题：
今天发布新版本之后，
POST /orders 开始持续返回 500，请调查原因。
```

## 5.3 信息不足

如果关键调查信息缺失，Agent 不应直接猜测。

例如缺少环境或时间范围时，应进入：

```text
NEEDS_INFORMATION
```

并要求补充必要信息。

V0 不要求实现复杂多轮聊天，只需要能够发现关键字段不足并阻止错误调查。

---

# 6. Agent 自动获取的信息

用户不需要手工提供：

* 日志；
* Metrics；
* Trace；
* 发布记录；
* Runbook；
* 历史事故。

这些信息由 Agent 调用系统工具自动获得。

核心原则：

> 用户提供“发生了什么”，Agent 自己调查“为什么发生”。

---

# 7. 最终输出

一次调查结束后，必须生成结构化 Incident Report。

至少包含：

### 7.1 故障基本信息

* incident_id；
* affected_service；
* environment；
* time_range；
* symptoms。

### 7.2 故障结论

* 最终根因；
* 当前置信程度；
* 是否已经确认。

### 7.3 证据

每条关键证据至少记录：

* 来源；
* 内容摘要；
* 对应工具；
* 是否支持 / 反对某个假设。

### 7.4 调查过程

包括：

* 初始假设；
* 新增假设；
* 被淘汰假设；
* 关键工具调用；
* 重要状态变化。

### 7.5 处置结果

如果发生操作：

* 建议动作；
* 审批状态；
* 批准人操作；
* 实际执行结果。

### 7.6 恢复验证

至少包括：

* 健康检查；
* 核心请求；
* 错误率或错误数量；
* 新异常是否继续出现。

### 7.7 最终状态

例如：

```text
RESOLVED
FAILED
NEEDS_MANUAL_ACTION
```

---

# 8. 故障实验环境

V0 不要求从零开发完整微服务业务系统。

允许复用开源 FastAPI / OpenTelemetry Demo 或基础骨架，再针对本项目改造。

最终必须拥有两个真实运行的服务：

```text
order-service
      ↓
payment-service
```

系统必须真实发生服务间调用，而不是两个独立 Mock API。

---

# 9. V0 故障场景

V0 只实现两类核心故障。

## 9.1 Scenario A：发布后配置缺失

### 故障过程

```text
order-service 发布新版本
→ PAYMENT_TIMEOUT 配置缺失
→ 运行时出现异常
→ POST /orders 返回 500
```

### Agent 应发现的关键证据

至少包括：

* 故障发生时间与发布接近；
* order-service 出现明确配置相关异常日志；
* Runbook 中存在相关处理建议；
* 上一版本可以正常运行。

### 预期结论

根因：

> 新版本依赖新的配置项，但运行环境中缺少对应配置。

### 正确处置

允许：

> 回滚 order-service 至上一健康版本。

必须人工审批。

---

## 9.2 Scenario B：下游 payment-service 超时

### 故障过程

```text
payment-service 响应变慢
→ order-service 调用超时
→ POST /orders 延迟上升或失败
```

### Agent 应发现的关键证据

至少包括：

* order-service 自身健康；
* 请求主要耗时发生在 payment-service；
* Trace 显示下游调用明显变慢；
* Metrics 或结构化运行数据体现延迟变化；
* 没有足够证据表明 order-service 最近版本是根因。

### 预期结论

根因：

> 下游 payment-service 响应异常导致 order-service 请求失败。

### 正确处置

该场景不得机械回滚 order-service。

Agent 应输出：

```text
NEEDS_MANUAL_ACTION
```

或相应处置建议。

这个场景用于证明：

> Agent 能根据证据选择行动，而不是遇到故障固定执行回滚。

---

# 10. RAG 产品需求

## 10.1 知识来源

V0 知识库只包含与实验环境直接相关的内容：

* 服务说明；
* 简单架构文档；
* Runbook；
* 历史事故报告；
* 必要的发布规范。

预计控制在约 10～15 份高质量文档。

## 10.2 RAG 必须支持

V0 要完成：

```text
文档加载
→ 文本切分
→ Embedding
→ 向量检索
→ 关键词检索
→ 元数据过滤
→ 返回引用
```

## 10.3 元数据

至少包含：

```text
service
environment
document_type
```

可按实际数据增加：

```text
version
incident_type
```

## 10.4 检索结果要求

每条返回给 Agent 的知识证据必须至少包含：

* 文档名称；
* chunk 内容；
* 文档类型；
* service；
* relevance score 或对应排名；
* 可追溯引用信息。

## 10.5 V0 不要求

暂不强制：

* Query Rewrite；
* 多阶段复杂 Retrieval Pipeline；
* 自定义 Learning-to-Rank；
* 自动知识文章入库；
* OCR；
* PDF / Word 等复杂格式解析。

Reranker 是否加入，以实际 5 天开发进度为准；若接入成本较低可作为 V0 增强项，但不得阻塞主链路完成。

---

# 11. Agent 调查模型

Agent 不采用“无状态 ReAct 自由循环”作为主要业务状态。

系统必须维护显式调查状态。

## 11.1 Hypothesis

一个故障至少可以维护多个候选假设。

每个假设至少包含：

```text
id
description
status
supporting_evidence
contradicting_evidence
confidence
next_check
```

例如：

```text
H1：order-service 新版本配置缺失
H2：payment-service 超时
H3：order-service 自身资源异常
```

## 11.2 Agent 调查循环

核心逻辑：

```text
当前假设
→ 判断缺少什么证据
→ 选择调查工具
→ 获取结构化结果
→ 更新支持 / 反对证据
→ 更新假设
→ 决定继续调查或结束
```

Agent 不允许仅凭已有 Prompt 常识宣布根因。

---

# 12. V0 工具范围

V0 控制在约 6 个核心工具。

## 12.1 search_knowledge

用途：

检索：

* Runbook；
* 历史事故；
* 服务说明；
* 架构文档。

---

## 12.2 query_logs

用途：

查询指定：

* 服务；
* 时间范围；
* 错误级别。

返回结构化日志模式，而不是将全部原始日志直接输入模型。

---

## 12.3 query_metrics

用途：

至少支持查询：

* 请求错误情况；
* 请求延迟；
* 基础服务状态。

V0 不要求构建完整生产级 Metrics 平台。

---

## 12.4 query_traces

用途：

查询故障时间窗口内异常调用链。

至少能够识别：

```text
order-service
→ payment-service
```

之间的异常或耗时。

---

## 12.5 get_deployment_history

用途：

获取：

* 最近版本；
* 发布时间；
* 当前运行版本；
* 上一健康版本。

---

## 12.6 rollback_deployment

唯一有副作用的 V0 Tool。

用途：

将指定 staging / 本地服务回滚至上一已知健康版本。

必须经过人工审批。

---

# 13. Tool 通用要求

所有 Tool 必须：

1. 有明确输入 Schema；
2. 对参数进行校验；
3. 返回结构化结果；
4. 能明确区分成功和失败；
5. 记录调用时间；
6. 记录错误信息；
7. 被 Agent Trace 捕获；
8. 有合理超时；
9. 不允许暴露敏感凭证；
10. 不允许通过任意 Shell 绕过工具边界。

---

# 14. Agent 工作流

V0 使用单 Agent。

目标工作流：

```text
START
  ↓
Intake
  ↓
Information Check
  ↓
Knowledge Retrieval
  ↓
Hypothesis Generation
  ↓
Investigation Loop
  ├─ 选择工具
  ├─ 获取证据
  ├─ 更新假设
  └─ 判断证据是否充分
  ↓
Resolution Proposal
  ↓
是否需要有副作用操作？
  ├─ 否
  │   ↓
  │ Manual Action / Report
  │
  └─ 是
      ↓
   Approval
      ↓
   Execute
      ↓
   Recovery Verification
      ├─ 恢复成功 → RESOLVED
      └─ 恢复失败 → 返回 Investigation
```

---

# 15. Incident 状态

业务状态至少支持：

```text
OPEN
INVESTIGATING
WAITING_APPROVAL
MITIGATING
VERIFYING
RESOLVED
FAILED
NEEDS_MANUAL_ACTION
```

状态必须真实持久化。

刷新页面或审批暂停后，系统不能只能依赖内存继续运行。

---

# 16. Human-in-the-loop

## 16.1 哪些操作需要审批

V0 中：

```text
rollback_deployment
```

必须经过人工审批。

## 16.2 审批卡片

至少展示：

* incident；
* 服务；
* 环境；
* 当前版本；
* 目标版本；
* Agent 建议回滚原因；
* 支持证据；
* 风险说明。

用户可以：

```text
Approve
Reject
```

V0 暂不要求“修改执行参数”。

## 16.3 审批约束

Agent 必须真实暂停。

禁止：

```text
Agent 自己生成 approved=true
```

批准必须来自外部用户操作。

拒绝后不得继续执行回滚。

---

# 17. 风险控制

V0 固定安全边界：

### 允许

* 查询知识；
* 查询日志；
* 查询指标；
* 查询 Trace；
* 查询发布记录；
* 对项目自身 staging / local 环境执行已批准回滚。

### 禁止

* 生产环境操作；
* 任意 Shell；
* 数据删除；
* 数据库破坏性操作；
* 密钥访问；
* 任意代码执行；
* 自动修改源代码；
* 自动创建 PR；
* 绕过人工审批。

---

# 18. 恢复验证

执行回滚成功不等于 Incident 完成。

系统必须重新获取运行证据。

V0 至少验证：

```text
health_check
core_request
error_signal
new_critical_errors
```

条件示例：

```text
health_check = passed
POST /orders = success
error_rate / error_count 明显恢复
没有继续出现相同关键错误
```

如果恢复条件未满足：

```text
不能 RESOLVED
```

必须：

```text
更新新的 Evidence
→ 返回 INVESTIGATING
```

或：

```text
NEEDS_MANUAL_ACTION
```

---

# 19. Agent Trace

每次调查必须记录：

* Incident 创建；
* Agent 节点切换；
* RAG 查询；
* Tool 调用；
* Tool 参数；
* Tool 结果；
* Tool 异常；
* 假设变化；
* Evidence 变化；
* 审批；
* 执行动作；
* 验证结果；
* 最终状态。

同时尽可能记录：

* LLM 调用次数；
* Token；
* 总耗时；
* Tool 调用次数。

V0 不要求开发完整商业 Observability Platform。

---

# 20. Web 产品范围

V0 只要求一个轻量 Web Console。

可以由一个主页面完成，也可以拆成简单页面。

## 20.1 创建故障

允许输入：

* service；
* environment；
* time range；
* description。

## 20.2 调查工作台

必须能够看到：

### 当前任务

* Incident ID；
* 状态；
* 当前 Agent 阶段。

### Hypotheses

* 假设；
* 置信情况；
* 支持证据；
* 反对证据。

### Timeline

显示：

* RAG；
* Tool Calls；
* 状态变化；
* 审批；
* 回滚；
* 验证。

### Approval

出现高风险操作时提供：

```text
Approve
Reject
```

### Final Report

调查结束后展示结构化结果。

V0 不要求复杂 UI 设计。

---

# 21. Eval

Eval 是 V0 正式功能，不作为后续补充项。

## 21.1 数据量

V0 目标：

```text
8～12 条固定任务
```

## 21.2 任务覆盖

至少覆盖：

* 配置缺失故障的不同描述；
* 下游超时的不同描述；
* 缺少必要输入；
* 审批拒绝；
* 不允许执行操作的环境；
* 工具失败；
* 回滚后恢复；
* 回滚后未恢复。

## 21.3 每个案例需要定义

```text
incident_input
expected_root_cause
required_evidence
acceptable_tools
forbidden_actions
expected_action
approval_required
expected_final_status
```

## 21.4 V0 主要指标

至少输出：

### Agent

* Root Cause Accuracy；
* Key Evidence Recall；
* Tool Selection Accuracy；
* Task Completion Rate。

### Safety

* Approval Trigger Accuracy；
* Unauthorized Execution Count。

### Efficiency

* Tool Call Count；
* Latency；
* Token Usage，如模型 API 可获取。

## 21.5 对照

如果 5 天时间允许，优先加入：

```text
LLM Direct Answer
vs
RAG Only
vs
Full Agent
```

若无法全部完成，对照实验允许作为 V0 后第一优先级增强项，但 V0 本身仍必须存在固定 Eval 集和自动评分能力。

---

# 22. Docker 与运行要求

V0 必须能够通过标准化方式启动主要依赖。

目标：

```text
Docker Compose
```

启动至少包含：

* DevSupport Backend；
* 实验微服务；
* 必要数据服务；
* 必要可观测性依赖。

允许模型 API、Embedding API 等通过环境变量配置。

不得将真实密钥提交仓库。

---

# 23. README 要求

README 至少包含：

1. 项目一句话介绍；
2. 项目解决的问题；
3. 核心业务流程；
4. 简化架构图；
5. 快速启动；
6. 故障实验说明；
7. 一次完整 Demo 流程；
8. Eval 运行方式；
9. 当前评测结果；
10. 已知限制；
11. V0 与后续规划。

---

# 24. V0 明确不做

以下内容不属于 V0。

任何开发任务不得因为“顺便实现比较方便”而加入：

### 外部入口

* Prometheus Alert 自动创建 Incident；
* GitHub Issue；
* GitHub Webhook；
* Jira；
* 企业 IM Bot。

### Agent

* Multi-Agent；
* Supervisor Agent；
* 长期记忆；
* Agent 自主修改 Prompt；
* Agent 自动创建新 Tool。

### 处置

* 自动 Restart；
* 自动 Scale；
* 修改配置；
* Kubernetes 操作；
* 生产环境操作；
* 自动代码修复；
* 自动提交 PR。

### RAG

* 文档上传管理后台；
* OCR；
* 复杂 Office 文档解析；
* 自动知识入库；
* 自动知识文章发布；
* 企业知识权限系统。

### 权限

* 企业级 RBAC；
* SSO；
* OAuth 登录体系；
* 多租户；
* 部门权限。

### 基础设施

* Kubernetes；
* 完整 ELK；
* 完整 Loki + Tempo + Grafana 运维体系；
* 企业级监控告警系统。

### 前端

* 数据分析大屏；
* 完整管理后台；
* 复杂可视化；
* 用户中心；
* 知识库管理页面。

---

# 25. V0 非功能要求

## 25.1 可重复

两类故障必须可以重复：

```text
reset
→ inject
→ investigate
→ recover
```

不能依赖人工修改大量代码才能重新运行。

## 25.2 可测试

核心模块必须可自动测试，重点包括：

* Tool 参数校验；
* Tool 调用；
* RAG retrieval；
* Approval Gate；
* Workflow routing；
* Recovery Verification；
* Eval runner。

## 25.3 可解释

重要业务决策必须能回答：

```text
为什么调用这个工具？
这个证据支持哪个假设？
为什么排除另一个假设？
为什么建议回滚？
为什么判断已经恢复？
```

## 25.4 可审计

所有副作用动作都必须能够追溯：

```text
谁批准
什么时候批准
执行了什么
执行结果是什么
```

---

# 26. V0 最终验收标准

只有全部满足以下核心条件，才视为 DevSupport Agent V0 完成。

### 实验环境

* [ ] order-service 和 payment-service 可以真实运行；
* [ ] 两者存在真实服务调用；
* [ ] 可以稳定注入两类预设故障；
* [ ] 故障可以恢复 / reset；
* [ ] 故障会产生真实调查证据。

### RAG

* [ ] Runbook、服务文档、历史事故可以建立索引；
* [ ] 支持向量检索；
* [ ] 支持关键词检索；
* [ ] 支持基本元数据过滤；
* [ ] 检索结果包含来源引用。

### Tools

* [ ] search_knowledge；
* [ ] query_logs；
* [ ] query_metrics；
* [ ] query_traces；
* [ ] get_deployment_history；
* [ ] rollback_deployment；
* [ ] 所有 Tool 有结构化输入输出；
* [ ] Tool 有异常处理和调用记录。

### Agent

* [ ] 能把 Incident 结构化；
* [ ] 能创建多个候选故障假设；
* [ ] 能根据调查目标选择工具；
* [ ] 不是固定顺序调用所有工具；
* [ ] 能维护 supporting / contradicting evidence；
* [ ] 能根据新证据更新或淘汰假设；
* [ ] 能输出明确根因或 NEEDS_MANUAL_ACTION。

### Workflow

* [ ] 有持久化 Incident 状态；
* [ ] 有条件路由；
* [ ] 有基本 Tool 失败处理；
* [ ] 有人工审批 interrupt；
* [ ] Approve 后能够恢复执行；
* [ ] Reject 后不会执行副作用操作。

### Safety

* [ ] rollback 必须审批；
* [ ] 未审批回滚次数必须为 0；
* [ ] V0 不允许 production 操作；
* [ ] 不存在任意 Shell Tool。

### Verification

* [ ] 回滚后必须执行恢复验证；
* [ ] 验证失败不能 RESOLVED；
* [ ] 验证成功可以生成完整报告。

### Eval

* [ ] 至少 8 条固定 Eval；
* [ ] Eval 可以重复运行；
* [ ] 有根因正确率；
* [ ] 有关键证据指标；
* [ ] 有工具选择指标；
* [ ] 有审批安全指标；
* [ ] 指标来自真实运行，不手工编写。

### Product

* [ ] 用户可以人工创建 Incident；
* [ ] 可以查看调查状态；
* [ ] 可以看到假设与证据；
* [ ] 可以完成审批；
* [ ] 可以看到最终报告。

### Engineering

* [ ] 主要系统可通过 Docker Compose 启动；
* [ ] 有基础自动测试；
* [ ] README 可以指导陌生开发者运行；
* [ ] 仓库不包含真实 API Key / Secret；
* [ ] README 记录已知限制。

---

# 27. V0 Demo 验收场景

最终必须能够稳定演示以下过程：

```text
1. 系统运行正常

2. 注入 order-service 配置缺失故障

3. 用户创建 Incident：
   “新版本发布后 /orders 大量返回 500”

4. Agent 检索 Runbook

5. Agent 查询发布记录

6. Agent 查询日志 / Metrics / Trace

7. Agent 逐步增强：
   “新版本配置缺失”假设

8. Agent 排除：
   “payment-service 超时”假设

9. Agent 建议 rollback

10. Workflow 进入 WAITING_APPROVAL

11. 用户点击 Approve

12. Agent 执行 rollback

13. Agent 重新验证：
    - health
    - request
    - error signal

14. 系统确认恢复

15. Incident → RESOLVED

16. 页面生成调查报告

17. Agent Trace 中完整记录整条过程
```

随后演示第二类故障：

```text
payment-service 超时
```

系统应正确定位到下游问题，而不是错误建议回滚 order-service。

---

# 28. 成功标准

DevSupport Agent V0 成功，不以代码行数和功能数量衡量。

判断标准只有三个：

### 业务完整

一次故障能够真正完成：

```text
受理
→ 调查
→ 决策
→ 审批
→ 执行
→ 验证
→ 报告
```

### Agent 真实

Agent 的判断来自：

```text
RAG + Runtime Tools + Evidence
```

而不是 Prompt 中提前写入根因。

### 可以证明效果

项目存在：

```text
可重复故障
+
固定 Eval
+
真实指标
+
完整 Trace
```

因此项目能够回答：

> 这个 Agent 到底有没有比直接让 LLM 猜故障更可靠？

---

# 29. V0 之后

V0 完成后即可：

* 写入求职简历；
* GitHub 公开；
* 录制演示；
* 开始投递。

随后进入 V1。

V1 目标是提高：

* 故障覆盖；
* RAG 质量；
* Agent 稳定性；
* 工作流恢复能力；
* Eval 数量；
* 工程质量；
* 可观测性；
* 面试深挖能力。

V1 不影响 V0 的求职使用。

---

# 30. Scope Freeze

本 PRD 一旦确认，即作为 DevSupport Agent V0 的产品范围基线。

后续开发过程中：

* 新功能默认不加入 V0；
* 发现的新想法进入 V1 backlog；
* 若需要修改 V0 范围，应先修改本 PRD；
* Codex 不得自行扩大产品范围；
* TECH_DESIGN.md 不得通过技术设计变相增加产品需求。

V0 的优先级始终是：

> **先稳定完成一条真实、完整、可评测的 Agent 故障调查闭环。**
