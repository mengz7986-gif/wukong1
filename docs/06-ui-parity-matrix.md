# 旧后台 UI 与功能一致性矩阵

验收口径：必须同时一致于旧后台的页面结构、入口、筛选、按钮、弹窗、取消路径、保存刷新、权限拒绝、部分失败反馈和业务行为。只有导航入口或相似视觉不算迁移完成。

| 页面 | 旧结构依据 | 新项目状态 | 完成条件 |
|---|---|---|---|
| 全局框架 | `base.html`、`sidebar.html`、`navbar.html`、三套 CSS | 进行中 | 桌面侧栏、移动抽屉、顶栏、会话、退出、390/1440 实测一致 |
| 仪表板 | `admin/index.html` | 进行中 | 指标、快捷入口、空态及权限一致 |
| 订单管理 | `admin/orders.html`、`orders.js` | 未迁移 | 今日指标、筛选、选择、导出、状态、弹窗和异常反馈一致 |
| 商品管理 | `admin/products.html`、`products.js` | 进行中 | 分类展开、筛选、批量添加、归档、库存、价格/预警弹窗一致 |
| 用户管理 | `admin/users.html`、`users.js` | 未迁移 | 用户筛选、资料、等级、价格、申请审核及权限一致 |
| 仓库管理 | `admin/warehouses.html`、`warehouses.js` | 进行中 | 新增、停启、确认取消、品类关系一致 |
| 到货入库 | `admin/inventory.html`、`inventory.js`、`inventory_operations.js` | 未迁移 | 批次、批量出入库、FIFO、整笔撤销一致 |
| 人工收账 | `admin/finance.html`、`finance.js` | 未迁移 | 收款、预存、应收、账单及通知分离一致 |
| 经营统计 | `admin/stats.html`、`stats.js` | 未迁移 | 北京时间筛选、统计和导出一致 |
| 待处理中心 | `admin/queue_recovery.html` | 未迁移 | 任务事实、unknown、查询后恢复和权限一致 |
| 异常地址库 | `admin/blocked_addresses.html`、`blocked_addresses.js` | 未迁移 | 查询、审核与地址证据边界一致 |
| 智能接单中心 | `admin/ai_settings.html`、`admin/rules.html`、学习 JS | 未迁移 | 设置、候选、审核、版本、撤销和18点复盘一致 |
| 运费与接单设置 | `admin/settings.html`、仓库运费 JS | 进行中 | 运费、接单、单位、仓库配置及审计一致 |
| 操作审计 | 旧审计入口与需求矩阵 | 进行中 | 实际操作者、前后值、结果、操作ID、关联实体和筛选一致 |

禁止把“入口已显示”“静态页面相似”或“后端模块单测通过”标成该页面完成。
