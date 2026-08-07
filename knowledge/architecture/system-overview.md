---
document_id: arch-system-overview
service: platform
environment: common
document_type: architecture
source: engineering-architecture-handbook
---

# DevSupport Fault Lab 系统概览

## 目的与边界

Fault Lab 是用于训练和验证故障调查流程的本地微服务环境。它由调用方
`order-service`、下游 `payment-service` 和面向调查的后台能力组成。生产
事故的结论不能仅来自这份文档；调查人员需要把文档中的可能性与运行时
日志、指标、调用链和部署事实结合。

## 请求路径

客户端通过 `POST /orders` 创建订单。order-service 生成订单标识，将金额和
订单标识传给 payment-service 的 `POST /payments`，并在收到有效的批准响应后
返回确认结果。请求标识会在服务间传递，trace 用于关联同一次调用。

## 可观察性和状态

两个服务均暴露健康、内部指标和部署事实接口。健康检查只说明进程可响应；
它不代表订单链路、下游依赖或当前配置都正确。部署事实和运行时故障状态
是不同的信号：排查发布后问题时应分别核对版本变化、错误模式和下游耗时。

## 调查原则

先限定受影响服务、环境和时间窗，再按照请求标识关联证据。对于 5xx，至少
区分调用方本地失败、下游失败和输入/响应校验失败。对于慢请求，使用 trace
确认时间实际消耗在哪个 span；不要仅凭调用方的总延迟将变更回滚。
