---
document_id: rb-order-service-500-triage
service: order-service
environment: local
document_type: runbook
source: order-service-oncall-runbook
---

# order-service 5xx 排查 Runbook

## 现象

调用方报告 `POST /orders` 返回 5xx，或 order-service 错误计数在某一时间窗内
上升。健康检查仍为成功时，不能据此排除订单路径故障。

## 首先收集

1. 记录受影响环境、开始时间、请求标识和大致比例。
2. 用请求标识查询 order-service 日志，确认错误发生在本地预检、下游调用还是
   下游响应校验之后。
3. 查看同一窗口的请求、成功、错误计数和最近耗时。
4. 查询部署事实并把部署时间与错误开始时间对齐，但不要把时间相关当作因果。

## 分支排查

- 若日志指向运行参数、必填设置或本地初始化，核对该环境的配置来源、键名、
  注入方式和最近配置变更；再确认付款调用没有发生。
- 若存在下游服务和失败/超时记录，按下游超时 Runbook 关联 trace 与依赖指标。
- 若下游返回成功但响应不符合约定，保留响应摘要并检查契约或兼容性变更。

## 处置与退出条件

先以安全方式验证配置修正或依赖恢复后，成功率和延迟是否恢复。只有当已确认
某次发布造成持续且高影响的本地回归、存在已知安全版本且审批已完成时，才将
回滚作为选项。不要在证据显示问题位于下游时回滚 order-service。
