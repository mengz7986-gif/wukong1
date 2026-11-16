# 旧项目只读来源索引

本索引只记录结构和规则的出处，不复制敏感内容或运行数据。所有路径均位于旧项目 `rc_unified_next_20261018`，仅供重建设计核对。

| 主题 | 只读出处 | 新项目处理 |
|---|---|---|
| 85 项台账 | `evidence/requirements-resumed-85.json` → `requirements[]` | 仅提取 ID、分组、状态、业务意图生成追溯矩阵 |
| 冻结边界 | `evidence/remaining074-frozen-matrix.json` | 只核对范围/状态，不复制历史业务证据 |
| 主 API 路由 | `candidate/api.py` | 作为兼容入口清单，不照搬单体实现 |
| Bot 客户端契约 | `candidate/api_client.py`，特别是建单参数与来源查询 | 新 JSON 契约前保留适配层设计 |
| Telegram 流程 | `candidate/bot.py` | 只继承候选判断、预览/确认、Validator、引用回执等业务边界 |
| 本地订单解析 | `candidate/order_parser.py`、`candidate/parser_rules.py` | 在新 recognition 模块逐条重建并以脱敏样例验收 |
| 提交防护 | `candidate/order_submission_guard.py`、`candidate/order_recording.py` | 采用稳定任务键、来源唯一键、事务重验 |
| 商品与别名 | `candidate/brand_aliases.py`、`candidate/flavor_aliases.py`、`candidate/alias_policy.py`、`candidate/rule_dictionary.py` | 后续仅导入经批准的脱敏 effective 规则；pending 不启用 |
| DeepSeek | `candidate/deepseek_client.py`、`candidate/deepseek_resolver.py`、`candidate/ai_transport.py` | adapter 默认 off；不复制 Key、账本或调用缓存 |
| 阿里云地址 | `candidate/aliyun_address_provider.py`、`candidate/aliyun_address_service.py`、`candidate/aliyun_address_settings.py` | 保留 500/日、超时和业务结果校验边界；默认 off |
| 仓库与 FIFO | `candidate/warehouse_allocation.py`、`candidate/inventory_operations.py`、`candidate/inventory_costs.py` | 新事务模型重新实现，不复制库存/成本数据 |
| 分单 | `candidate/split_management.py`、`candidate/address_daily_orders.py` | 北京时间自然日、前序成功、额度和幂等规则重新实现 |
| 导出与互斥 | `candidate/order_export.py`、`candidate/order_selection.py` | 以独立录单事实和导出批次重建 |
| 财务 | `candidate/payment_history.py`、`candidate/manual_receivables.py`、`candidate/cumulative_bill_snapshot.py` | payment 外键、余额前后链和操作键重新实现 |
| 通知 | `candidate/telegram_delivery.py`、`candidate/telegram_delivery_state.py`、`candidate/account_notifications.py` | 新空 Outbox；旧任务不迁移，unknown 不自动重发 |
| 权限 | `candidate/admin_security.py`、`candidate/web_security.py` | 逐接口角色和有效载荷重新验收 |
| 学习 | `candidate/learning_pipeline.py`、`candidate/learning_review.py`、`candidate/learning_daily_service.py` | 候选/审核/版本/撤销，18:00 后安全空闲；默认无 AI |
| 物流退役证据 | `candidate/fhd_client.py` 及 `candidate/api.py` 中相关拒绝路径 | 明确不恢复风火递 |

旧 `candidate/orders.db*`、JSON 用量/诊断账本、审计原文、coordination 报告及知识库整目录均不复制。后续若需定位规则，只读取最小必要文件并记录出处与审核状态。
