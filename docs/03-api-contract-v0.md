# API 契约 v0（设计稿，未开放网络服务）

首版骨架只实现领域服务与 CLI，不启动 HTTP 或 Telegram 监听。此契约用于先冻结边界。

## 身份

- 后台：登录会话＋CSRF；服务调用：最小权限服务凭证。
- 客户 Telegram ID、内部客户 ID、服务凭证为不同身份，不可互换。
- `actor_id` 从认证上下文产生，禁止相信请求体自报操作者。

## 核心端点

| 方法 | 路径 | 幂等 | 角色 | 说明 |
|---|---|---|---|---|
| POST | `/v1/order-drafts/parse` | task key | approved customer/service | 本地优先识别，候选别名不生效 |
| GET | `/v1/order-drafts/{id}/preview` | read | owner/admin/customer owner | 返回目录版本、报价版本及校验错误 |
| POST | `/v1/order-drafts/{id}/confirm` | required | customer owner/admin proxy | 事务内重验授权、报价、库存及执行凭证 |
| GET | `/v1/submissions/by-source` | read | service/admin | 超时后按来源查提交结果 |
| POST | `/v1/payments` | operation key | finance privilege/service | payment 独立实体；余额流水外键关联 |
| POST | `/v1/exports` | operation key | recording privilege | 成功后互斥迁移录单事实 |
| POST | `/v1/learning-candidates/{id}/review` | operation key | owner/reviewer | 审核后才可发布规则版本 |

## 建单载荷原则

新接口使用 JSON，商品使用稳定 `product_id`；兼容适配层以后负责把旧 Form＋JSON 字符串转换为新结构。请求必须含来源身份、来源消息、任务幂等键、目录版本和报价快照。确认端不相信预览结果，必须重验。

## 结果原则

业务提交结果与消息送达结果分离。提交成功后写 Outbox；响应含订单与子单标识，但不得以 Telegram 返回替代订单提交事实。所有错误返回稳定业务码和面向客户的具体提示，技术链脱敏保存。

## 外部调用边界

DeepSeek、阿里云、Telegram、汇率均经 adapter；默认 `off`。缓存只覆盖同任务、身份和目录版本下的识别结果，绝不缓存最终授权、库存或价格。阿里云保留北京时间每日 500 次配置位，但默认关闭，不能自动扩额或充值。
