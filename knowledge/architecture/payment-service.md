---
document_id: arch-payment-service
service: payment-service
environment: common
document_type: architecture
source: payment-service-service-catalog
---

# payment-service 服务说明

## 职责

payment-service 接收订单标识与正数金额，并返回付款处理结果。它是
order-service 的同步下游，因而其响应时间会直接影响订单链路的端到端延迟。
请求标识和 trace 上下文用于把付款处理与上游订单请求关联起来。

## 可用性语义

`/health` 仅检查服务能否响应。服务健康并不保证每笔付款都能在上游客户端的
等待时间内完成：排队、外部依赖慢、资源竞争或人为延迟都可能使请求变慢。
部署状态同样与运行时延迟分开记录，因此稳定版本也可能在没有发布的情况下
表现出高延迟。

## 调查信号

内部 metrics 提供请求数量、成功/错误数量和平均、最近请求耗时。调查下游超时
时，应将 payment-service 的耗时和 order-service 的失败窗口对齐，并用 trace
确认两个 span 属于相同请求。单独观察到 order-service 的 502 不能区分网络、
超时或不合法下游响应。

## 处置边界

不要因为调用方报错就回滚调用方服务。若付款服务变慢，应优先收集依赖侧延迟、
资源与近期变更证据；恢复操作需根据实际归因和既定变更流程执行。
