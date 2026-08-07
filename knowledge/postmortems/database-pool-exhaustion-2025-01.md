---
document_id: pm-database-pool-exhaustion-2025-01
service: order-service
environment: production
document_type: postmortem
source: incident-review-2025-01-database-pool
---

# 干扰复盘：数据库连接池耗尽（2025-01）

## 摘要

历史生产事件中，订单服务延迟和 5xx 同时上升。问题发生在本地数据库连接获取
阶段，付款服务没有普遍的延迟异常。它与“订单失败且健康检查正常”的表象相似，
但证据路径不同。

## 区分信号

调用方 trace 在访问下游前已经等待；连接池等待和数据库连接错误在本地日志中
出现。payment-service 的请求量和处理时长保持基线。扩容付款服务或回滚无关
发布都无法恢复该问题。

## 教训

相似的 HTTP 状态码不应直接映射为同一类事故。排查应明确区分本地资源等待、
配置问题与下游调用耗时，并选择能反驳替代解释的证据。
