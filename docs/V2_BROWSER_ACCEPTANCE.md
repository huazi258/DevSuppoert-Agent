# V2 浏览器验收

本记录是 M6.4 的真实浏览器验收，不替代 V2 的确定性 Eval 或 Adapter 集成验收。浏览器通过 Web 的 `/api` 代理访问 Backend 和 PostgreSQL；没有用直接 Backend 请求代替以下用户流程。

## 环境与安全边界

验收在 Compose 的隔离本地项目中运行，PostgreSQL、Backend 和 Web 均通过 health check。页面只展示部署配置所允许的一个 Target 和其一个 Service。验收数据使用本地受控的 Target、Service、Incident 和 Markdown 文档；artifact 仅记录安全对象 ID、状态和计数。

实例没有登录功能，因此仅在 localhost 的可信本地环境运行，不能直接暴露至公网。

## 实际流程与结果

| 步骤 | 浏览器观察 | 结果 |
| --- | --- | --- |
| 首页 | 中文只读调查首页可打开；Target / Service 下拉框只出现已配置白名单；未出现 Approval、rollback 或 recovery 入口。 | PASS |
| 创建 Incident | 仅填写 Target、Service、时间范围和观察现象；未要求根因、修复命令、URL 或 Token。 | PASS |
| 调查详情 | 调查进入安全的 `FAILED` 终态，显示结论、人工建议、Evidence / Citation、Timeline 与本轮持久化 Report；无“已恢复”或“已解决”文案，技术详情默认折叠。 | PASS |
| Round continuation | 从第 1 轮 terminal Incident 提交新 Observation 后创建第 2 轮；两轮都保留独立 Report，Observation 显示为“待验证观察”。 | PASS |
| Knowledge | `/knowledge` 的 Target、Scope、Service、Environment 可选择；真实上传一个 `.md` 后出现在列表，并可从启用切换为停用；页面不显示 Provider URL、Secret 或 embedding 字段。 | PASS |

本次受控环境刻意未提供可用 LLM Provider；系统将其有界失败收敛为 `FAILED / retry_budget_exhausted`，而没有伪造 `CONCLUDED` 或根因。这验证了浏览器中的安全终态、报告和续轮路径；不把该场景误报为真实模型调查成功。

`CONCLUDED` 的恢复语义未被伪造以覆盖本次浏览器场景。实际显示的 terminal 详情没有恢复或解决声明，且 V2 `CONCLUDED` 仅代表形成调查结论，不代表系统已恢复。

## Artifact

机器可读结果位于 `evals/results/v2-browser-acceptance.json`。其中不包含 Secret、Provider endpoint、raw logs、LLM prompt/response 或 Tool arguments。
