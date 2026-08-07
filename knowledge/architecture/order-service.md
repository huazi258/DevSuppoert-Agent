---
document_id: arch-order-service
service: order-service
environment: common
document_type: architecture
source: order-service-service-catalog
---

# order-service 服务说明

## 职责

order-service 接受有效金额的订单请求，并协调 payment-service 完成付款批准。
在付款响应的订单标识和状态均符合预期时，订单被标记为 confirmed。它不是
付款授权系统，也不应把下游的内部诊断细节直接返回给调用方。

## 依赖和错误边界

服务依赖 payment-service 的 HTTP 接口，并对该调用施加客户端超时。连接失败、
读超时、非成功 HTTP 响应和无法验证的付款响应都会造成订单链路失败，通常应
从 order-service 的下游调用日志和 trace 中确认。独立于下游调用之前的本地
校验或运行配置问题也可能产生 5xx；此时不应假定 payment-service 已收到请求。

## 可用调查信号

- 结构化日志包含请求标识、状态码、耗时和（适用时）下游服务信息。
- 内部 metrics 包含请求数、成功数、错误数和最近一次请求耗时。
- 部署接口只报告版本和部署时间，不能说明部署是否导致了当前症状。

## 处置提示

当发布与故障时间相关时，先比较发布前后错误率和请求路径。若证据表明失败
发生在本地预检阶段，检查配置和启动参数；若证据显示下游 span 占据主要耗时，
应调查依赖健康度。回滚是受控操作，必须在影响、因果证据和审批均满足时再考虑。
