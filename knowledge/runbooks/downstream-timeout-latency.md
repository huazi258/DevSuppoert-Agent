---
document_id: rb-downstream-timeout-latency
service: platform
environment: common
document_type: runbook
source: dependency-operations-runbook
---

# 下游 Timeout 与 Latency 排查 Runbook

## 现象

调用方请求变慢、收到网关类错误，或出现连接/读取超时。调用方的总耗时只能
说明用户体验受影响，不能说明耗时发生在调用方本身。

## 证据收集

1. 选取失败和成功请求各若干，按请求标识关联日志与 trace。
2. 比较调用方 span 与各下游 span 的持续时间、状态和重试情况。
3. 查询下游服务在同一时间窗的请求量、平均耗时、错误数及部署状态。
4. 核对客户端 timeout、连接池、DNS/网络错误和上游重试是否放大了负载。

## 常见解释

下游响应变慢、网络路径不稳定、下游饱和、客户端 timeout 过短或无效响应都可
呈现为相似的上游失败。应优先选择能解释 trace 中主要耗时位置的假设，再用
下游指标证实；不要因为上游服务刚发布就跳过依赖侧检查。

## 处置

若下游实际慢，协调下游值班人员并限制可能放大的重试。若 timeout 配置近期有
变更，评估其与服务目标的一致性。只有证据证明调用方版本在本地引入回归时，
才按审批流程评估调用方回滚；恢复后必须复测完整调用链。
