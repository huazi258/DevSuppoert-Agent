---
document_id: pm-payment-contract-mismatch-2025-03
service: payment-service
environment: production
document_type: postmortem
source: incident-review-2025-03-payment-contract
---

# 干扰复盘：付款响应契约不兼容（2025-03）

## 摘要

一次依赖接口演进后，payment-service 很快返回成功 HTTP 响应，但调用方拒绝了
响应内容并对用户呈现失败。该事件容易与下游不可用混淆，因为调用方同样记录
了下游调用失败。

## 区分信号

trace 显示下游 span 很短且状态成功；payment-service 指标也没有延迟峰值。关键
证据来自调用方的响应校验日志和捕获的字段差异。将客户端 timeout 调大或扩容
下游均没有帮助。

## 教训

调查下游相关错误时，需分别检查传输失败、慢响应和语义不兼容。保留经过脱敏的
响应摘要和契约版本，可使后续调查不依赖猜测。
