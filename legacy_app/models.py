"""
数据库模型
使用 SQLAlchemy ORM
"""

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey,
    create_engine, VARCHAR, UniqueConstraint, event, func, case
)
from typing import Optional, List, Dict, Tuple
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, Session
from dynamic_prices import price_property
from sqlalchemy.ext.hybrid import hybrid_property
from datetime import datetime, timedelta, timezone
import uuid
import re
import os
import math

try:
    from config import STOCK_AUTO_UNLIST_MODE
except Exception:
    STOCK_AUTO_UNLIST_MODE = "threshold"

Base = declarative_base()


def normalize_low_stock_threshold(value, default: int = 20) -> int:
    """少量预警值允许为 0；空值才回落到默认值。"""
    if value is None:
        return max(0, int(default))
    if isinstance(value, str) and value.strip() == "":
        return max(0, int(default))
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return max(0, int(default))


def stock_auto_unlist_mode() -> str:
    mode = (STOCK_AUTO_UNLIST_MODE or "threshold").strip().lower()
    if mode in ("zero", "stock_zero", "order"):
        return "zero"
    return "threshold"


def should_auto_unlist(total_stock: int, low_stock_threshold: int) -> bool:
    total = max(0, int(total_stock or 0))
    threshold = normalize_low_stock_threshold(low_stock_threshold)
    if stock_auto_unlist_mode() == "zero":
        return total <= 0
    return total <= threshold


def stock_auto_unlist_description(low_stock_threshold: int) -> str:
    threshold = normalize_low_stock_threshold(low_stock_threshold)
    if stock_auto_unlist_mode() == "zero":
        return "总库存为 0 时 TG 自动停售"
    return f"总库存小于等于预警值 {threshold} 时 TG 自动停售"


class User(Base):
    """用户表

    业务说明:
    - balance 视为预存/备用金，不再强制每单扣款
    - 实际欠款 = 未结清订单金额合计（由业务方法计算）
    - 每个用户可单独设置商品结算单价
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String(50), unique=True, index=True, nullable=False)
    username = Column(String(100), nullable=True)
    telegram_name = Column(String(150), nullable=True)  # Telegram 显示名（first_name + last_name）
    display_name = Column(String(100), nullable=True)  # 后台备注名/客户名
    balance = Column(Float, default=0.0, nullable=False)  # 预存余额（可选）
    agent_level = Column(Integer, default=0, nullable=False)  # 0=VIP客户, 1=超级VIP, 2=合伙人
    preferred_pay_method = Column(String(20), default="cny", nullable=False)  # cny/usdt/other
    remark = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    # 关系
    orders = relationship("Order", back_populates="user")
    product_prices = relationship("UserProductPrice", back_populates="user", cascade="all, delete-orphan")
    category_prices = relationship("UserCategoryPrice", back_populates="user", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="user", cascade="all, delete-orphan")
    dropship_auth_requests = relationship("DropshipAuthRequest", back_populates="user", cascade="all, delete-orphan")
    cancellation_requests = relationship("OrderCancellationRequest", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(telegram_id={self.telegram_id}, balance={self.balance}, agent_level={self.agent_level})>"

    def get_agent_level_name(self):
        """获取客户等级名称"""
        level_names = {0: "VIP客户", 1: "超级VIP", 2: "合伙人"}
        return level_names.get(self.agent_level, "未知")


class Warehouse(Base):
    """仓库表"""
    __tablename__ = "warehouses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    address = Column(Text, nullable=False)
    shipping_fee = Column(Float, default=0.0, nullable=False)  # 运费
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    stocks = relationship("WarehouseStock", back_populates="warehouse")

    def __repr__(self):
        return f"<Warehouse(name={self.name}, shipping_fee={self.shipping_fee})>"


class Product(Base):
    """商品表"""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), index=True, nullable=False)
    # 多级价格体系
    _cost_price = Column('cost_price', Float, nullable=False, default=0.0)
    cost_price = price_property('cost_price')
    _reference_price_state = Column('reference_price_state',String(20),nullable=True)
    @hybrid_property
    def reference_price_state(self):
        return __import__('dynamic_prices').effective_state(self)
    @reference_price_state.setter
    def reference_price_state(self,value):self._reference_price_state=value
    @reference_price_state.expression
    def reference_price_state(cls):return cls._reference_price_state
    _level1_agent_price = Column('level1_agent_price', Float, nullable=False, default=0.0)
    level1_agent_price = price_property('level1_agent_price')
    _level2_agent_price = Column('level2_agent_price', Float, nullable=False, default=0.0)
    level2_agent_price = price_property('level2_agent_price')
    _regular_price = Column('regular_price', Float, nullable=False)
    regular_price = price_property('regular_price')
    description = Column(Text, nullable=True)
    default_warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=True)  # 默认仓库
    # 父级商品分类
    parent_id = Column(Integer, ForeignKey("products.id"), nullable=True)  # 父级商品ID
    is_parent = Column(Boolean, default=False)  # 是否为父级分类（本身不售卖）
    low_stock_threshold = Column(Integer, default=20, nullable=False)  # 少量库存预警阈值
    category_serial = Column(Integer, nullable=True)  # 分类独立编号
    product_serial = Column(Integer, nullable=True)  # 商品独立编号
    auto_unlisted = Column(Boolean, default=False, nullable=False)  # 库存低于预警后 TG 机器人停售
    is_archived = Column(Boolean, default=False, nullable=False)  # 批量删除口味时软归档，保留历史订单引用
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    stocks = relationship("WarehouseStock", back_populates="product")
    order_items = relationship("OrderItem", back_populates="product")
    default_warehouse = relationship("Warehouse", foreign_keys=[default_warehouse_id])
    # 自引用关系：父级和子级
    parent = relationship("Product", remote_side=[id], backref="children")

    def __repr__(self):
        if self.is_parent:
            return f"<Product(name={self.name}, is_parent=True)>"
        return f"<Product(name={self.name}, regular_price={self.regular_price})>"

    def get_full_name(self) -> str:
        """获取完整名称（包含父级）"""
        if self.parent and not self.parent.is_parent:
            # 如果父级不是分类，继续向上查找
            return self.parent.get_full_name()
        elif self.parent:
            return f"{self.parent.name}-{self.name}"
        return self.name

    def get_price_for_agent_level(self, agent_level: int) -> float:
        """根据客户等级获取对应价格"""
        price_map = {
            1: self.level2_agent_price,  # 超级VIP
            2: self.level1_agent_price,  # 合伙人
        }
        return price_map.get(agent_level, self.regular_price)

    def calculate_profit(self, agent_level: int, quantity: int) -> float:
        """计算利润 = (售价 - 成本) * 数量"""
        selling_price = self.get_price_for_agent_level(agent_level)
        return (selling_price - self.cost_price) * quantity


class WarehouseStock(Base):
    """仓库库存表"""
    __tablename__ = "warehouse_stock"

    id = Column(Integer, primary_key=True, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, default=0, nullable=False)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    warehouse = relationship("Warehouse", back_populates="stocks")
    product = relationship("Product", back_populates="stocks")

    # 联合唯一约束
    __table_args__ = (
        {'extend_existing': True},
    )

    def __repr__(self):
        return f"<WarehouseStock(warehouse={self.warehouse.name}, product={self.product.name}, quantity={self.quantity})>"


class Order(Base):
    """订单表

    结账模式: 人工收账，不强制每单一结
    payment_status: unpaid/partial/paid
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    order_number = Column(String(50), unique=True, index=True, nullable=False)
    # Human-facing number.  ``order_number`` remains the globally unique,
    # rollback-compatible reference used by accounting and inventory facts.
    # The public value may repeat in a later ISO week (A1 ... G<n>).
    business_order_number = Column(String(50), nullable=True, index=True)
    total_amount = Column(Float, nullable=False)  # 商品总金额
    total_shipping_fee = Column(Float, default=0.0, nullable=False)  # 总运费
    grand_total = Column(Float, nullable=False)  # 总金额（含运费）
    total_profit = Column(Float, default=0.0, nullable=False)  # 总利润
    cost_status = Column(String(32), default='COST_COMPLETE', nullable=False)
    paid_amount = Column(Float, default=0.0, nullable=False)  # 已收金额
    payment_status = Column(String(20), default="unpaid", nullable=False)  # unpaid/partial/paid
    status = Column(String(20), default="pending")  # pending, split_pending, confirmed, shipped, cancelled
    split_group_id = Column(String(80), nullable=True, index=True)
    split_index = Column(Integer, nullable=True)
    split_count = Column(Integer, default=1, nullable=False)
    split_confirmed_at = Column(DateTime, nullable=True)
    recorded_at = Column(DateTime, nullable=True, index=True)
    recorded_export_batch_id = Column(String(64), nullable=True, index=True)
    recipient_name = Column(String(100), nullable=False)
    recipient_phone = Column(String(20), nullable=False)
    recipient_address = Column(Text, nullable=False)
    notes = Column(Text, nullable=True)
    # Telegram source message used by group auto-order receipts and payment notices.
    source_chat_id = Column(String(64), nullable=True, index=True)
    source_message_id = Column(Integer, nullable=True)
    source_chat_type = Column(String(20), nullable=True)
    source_chat_title = Column(String(255), nullable=True)
    fhd_push_status = Column(String(20), default="not_pushed", nullable=False)  # not_pushed/success/failed/partial
    fhd_push_mode = Column(String(50), nullable=True)
    fhd_push_count = Column(Integer, default=0, nullable=False)
    fhd_last_push_at = Column(DateTime, nullable=True)
    fhd_last_error = Column(Text, nullable=True)
    fhd_trade_id = Column(String(80), nullable=True)
    fhd_waybill_code = Column(String(100), nullable=True)
    fhd_cp_code = Column(String(50), nullable=True)
    fhd_cp_name = Column(String(100), nullable=True)
    shipped_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    cancellation_requests = relationship("OrderCancellationRequest", back_populates="order", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Order(id={self.id}, order_number={self.order_number}, status={self.status}, profit={self.total_profit})>"


class OrderNumberSequence(Base):
    """Per-business-day visible order-number counter.

    The public number is compact (``1022-1``). A row is kept for each Beijing
    calendar date, starting at 1. A new year can safely reuse the
    visible value because ``orders.order_number`` remains the internal unique
    reference and ``orders.business_order_number`` is display-only.
    """

    __tablename__ = "order_number_sequences"

    id = Column(Integer, primary_key=True, index=True)
    week_key = Column(String(12), unique=True, nullable=False, index=True)
    prefix = Column(String(1), nullable=False)
    next_number = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class OrderExportBatch(Base):
    """Immutable audit fact for one server-generated order export.

    A completed row means the server generated the complete file and committed
    every selected order to the same batch.  It does not claim that a browser
    saved the response after it left the server.
    """

    __tablename__ = "order_export_batches"

    id = Column(String(64), primary_key=True)
    selection_batch = Column(String(80), nullable=True, index=True)
    file_format = Column(String(12), nullable=False)
    result = Column(String(20), nullable=False, index=True)  # success/failed
    order_count = Column(Integer, nullable=False, default=0)
    duplicate_count = Column(Integer, nullable=False, default=0)
    content_sha256 = Column(String(64), nullable=True)
    error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True, index=True)


class OrderExportItem(Base):
    """Order membership of a completed/failed export attempt."""

    __tablename__ = "order_export_items"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(String(64), ForeignKey("order_export_batches.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    order_number = Column(String(50), nullable=False)
    was_duplicate = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("batch_id", "order_id", name="uq_order_export_batch_order"),
    )


class OrderCancellationRequest(Base):
    """客户取消订单申请；只有后台审核通过后才会真正回滚库存/余额。"""

    __tablename__ = "order_cancellation_requests"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    telegram_id = Column(String(50), nullable=False, index=True)
    order_number = Column(String(50), nullable=False, index=True)
    reason = Column(Text, nullable=True)
    status = Column(String(20), default="pending", nullable=False, index=True)
    admin_note = Column(Text, nullable=True)
    reviewed_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)

    order = relationship("Order", back_populates="cancellation_requests")
    user = relationship("User", back_populates="cancellation_requests")


class OrderItem(Base):
    """订单商品明细表"""
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False)  # 发货仓库
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    unit_cost = Column(Float, default=0.0, nullable=False)  # FIFO/批次成本单价
    cost_amount = Column(Float, default=0.0, nullable=False)  # 成本小计
    cost_status = Column(String(32), default='COST_COMPLETE', nullable=False)
    unknown_cost_quantity = Column(Integer, default=0, nullable=False)
    fifo_details = Column(Text, default='[]', nullable=False)
    shipping_fee = Column(Float, default=0.0, nullable=False)  # 该商品的运费
    profit = Column(Float, default=0.0, nullable=False)  # 该商品的利润

    # 关系
    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")
    warehouse = relationship("Warehouse")

    def __repr__(self):
        return f"<OrderItem(order={self.order.order_number}, product={self.product.name}, quantity={self.quantity}, profit={self.profit})>"


class FenghuodiPushLog(Base):
    """风火递推单日志：记录每次推送/重推/拆单结果。"""
    __tablename__ = "fenghuodi_push_logs"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    order_number = Column(String(80), nullable=True, index=True)
    split_mode = Column(String(20), default="none", nullable=False)
    split_key = Column(String(120), nullable=True)
    mode = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False)  # success/failed
    trade_id = Column(String(80), nullable=True)
    waybill_code = Column(String(100), nullable=True)
    cp_code = Column(String(50), nullable=True)
    cp_name = Column(String(100), nullable=True)
    request_payload = Column(Text, nullable=True)
    response_payload = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("Order")


class Transaction(Base):
    """交易记录表（预存余额流水，可选）"""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)  # 正数为充值，负数为消费
    type = Column(String(20), nullable=False)  # recharge, order, refund, adjust
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    user = relationship("User")

    def __repr__(self):
        return f"<Transaction(user={self.user_id}, amount={self.amount}, type={self.type})>"


class CatalogRenameAudit(Base):
    __tablename__ = 'catalog_rename_audit'
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    before_name = Column(String(100), nullable=False)
    after_name = Column(String(100), nullable=False)
    actor = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PriceOverrideAudit(Base):
    """Append-only reset evidence; never rewrites historical order prices."""
    __tablename__ = 'price_override_audit'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    actor = Column(String(100), nullable=False)
    before_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class UserProductPrice(Base):
    """用户专属结算单价"""
    __tablename__ = "user_product_prices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    unit_price = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="product_prices")
    product = relationship("Product")

    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='uq_user_product_price'),
        {'extend_existing': True},
    )


class UserCategoryPrice(Base):
    """Category fallback only; a flavor override always takes precedence."""
    __tablename__='user_category_prices'
    id=Column(Integer,primary_key=True)
    user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    parent_id=Column(Integer,ForeignKey('products.id'),nullable=False,index=True)
    unit_price=Column(Float,nullable=False)
    updated_at=Column(DateTime,default=datetime.utcnow,onupdate=datetime.utcnow)
    user=relationship('User',back_populates='category_prices')
    __table_args__=(UniqueConstraint('user_id','parent_id',name='uq_user_category_price'),)


class DropshipAuthRequest(Base):
    """用户代发下单授权申请"""
    __tablename__ = "dropship_auth_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    telegram_id = Column(String(50), nullable=False, index=True)
    username = Column(String(100), nullable=True)
    status = Column(String(20), default="pending", nullable=False, index=True)
    message = Column(Text, nullable=True)
    admin_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="dropship_auth_requests")


class Payment(Base):
    """人工收账记录

    method: cny / usdt / other
    可挂到某订单，也可只记到用户（冲减欠款）
    """
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    amount = Column(Float, nullable=False)
    method = Column(String(20), default="cny", nullable=False)  # cny/usdt/other
    original_amount = Column(Float, nullable=True)  # 原币数量；USDT 时为链上数量
    exchange_rate = Column(Float, nullable=True)  # 1 原币兑换多少 CNY
    exchange_currency = Column(String(20), nullable=True)  # CNY/USDT/OTHER
    note = Column(Text, nullable=True)
    operator = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="payments")
    order = relationship("Order")



class BlockedAddress(Base):
    """异常/诈骗地址黑名单：命中后禁止下单"""
    __tablename__ = "blocked_addresses"

    id = Column(Integer, primary_key=True, index=True)
    address_text = Column(Text, nullable=False)  # 原始录入
    normalized = Column(String(500), nullable=False, index=True)  # 规范化后用于匹配
    match_mode = Column(String(20), default="contains", nullable=False)  # exact/contains
    reason = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class StockMovement(Base):
    """库存流水：出库/入库/回滚，用于补货与发货统计；入库可带批次成本"""
    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False)
    change_qty = Column(Integer, nullable=False)  # +入库 / -出库
    movement_type = Column(String(20), nullable=False)  # inbound/order_out/cancel_in/adjust/delete
    unit_cost = Column(Float, nullable=True)  # 本批次成本（仅入库有意义）
    batch_no = Column(String(50), nullable=True)  # 批次号，可空自动生成
    remain_qty = Column(Integer, nullable=True)  # 该入库批次剩余可出库数量
    ref_type = Column(String(20), nullable=True)  # order/payment/manual/batch
    ref_id = Column(String(50), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product")
    warehouse = relationship("Warehouse")


class InventoryBatch(Base):
    """入库批次：同商品不同进价分开记账，出库按先进先出扣减"""
    __tablename__ = "inventory_batches"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    batch_no = Column(String(50), nullable=False, index=True)
    fifo_priority = Column(Integer, default=1, nullable=False)
    cost_source = Column(String(40), default='COST_UNVERIFIED', nullable=False)
    unit_cost = Column(Float, nullable=False, default=0.0)
    quantity = Column(Integer, nullable=False, default=0)  # 初始入库数
    remain_qty = Column(Integer, nullable=False, default=0)  # 剩余
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product")
    warehouse = relationship("Warehouse")


# 数据库连接管理
class Database:
    ORDER_NUMBER_PREFIXES = "ABCDEFG"

    def __init__(self, db_url: Optional[str] = None):
        db_url = db_url or os.getenv('DATABASE_URL', 'sqlite:///./orders.db')
        connect_args = {}
        engine_options = {}
        if "sqlite" in db_url:
            connect_args = {"check_same_thread": False, "timeout": 30}
            # In-memory SQLite databases are connection-local. Endpoint
            # handlers run in worker threads, so tests must reuse one DB.
            if db_url.rstrip('/').endswith(':memory:'):
                from sqlalchemy.pool import StaticPool
                engine_options['poolclass'] = StaticPool

        self.engine = create_engine(db_url, connect_args=connect_args, **engine_options)
        if "sqlite" in db_url:
            @event.listens_for(self.engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA foreign_keys=ON")
                finally:
                    cursor.close()

        Base.metadata.create_all(bind=self.engine)
        # Additive split scheduling facts; deployment validates this migration
        # on a fresh production copy before the whitelist is installed.
        from split_management import migrate as migrate_split_management
        with self.engine.begin() as connection:
            from order_recording import migrate as migrate_order_recording
            migrate_order_recording(connection)
            migrate_split_management(connection)
            from product_order_limits import migrate as migrate_product_order_limits
            migrate_product_order_limits(connection)
            from cancellation_facts import migrate as migrate_cancellation_facts
            migrate_cancellation_facts(connection)
            from manual_receivables import migrate as migrate_manual_receivables
            from bill_batches import migrate as migrate_bill_batches
            migrate_bill_batches(connection)
            migrate_manual_receivables(connection)
            from product_units import migrate as migrate_product_units
            migrate_product_units(connection)
            from proxy_confirmation import migrate as migrate_proxy_confirmation
            migrate_proxy_confirmation(connection)
            from order_submission_guard import migrate as migrate_submission_guard
            migrate_submission_guard(connection)
        self._ensure_performance_indexes()
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    @classmethod
    def _order_number_context(cls, now=None) -> Tuple[str, str]:
        """Year-qualified key and weekday letter + MMDD public prefix."""
        china_now = now or datetime.now(timezone(timedelta(hours=8)))
        if china_now.tzinfo is not None:
            china_now = china_now.astimezone(timezone(timedelta(hours=8)))
        return china_now.strftime('%Y-%m-%d'), chr(ord('A')+china_now.weekday())+china_now.strftime('%m%d')

    def _allocate_order_numbers(self, session: Session, count: int, now=None) -> Tuple[List[str], str]:
        """Allocate one visible base number inside the caller's transaction.

        ``count`` is retained for rollback/API compatibility but a logical
        order always consumes exactly one daily sequence value.  Split packages
        derive ``1022-1-1``, ``1022-1-2`` from that base.
        """
        week_key, prefix = self._order_number_context(now)
        sequence = (
            session.query(OrderNumberSequence)
            .filter(OrderNumberSequence.week_key == week_key)
            .first()
        )
        if sequence is None:
            sequence = OrderNumberSequence(
                week_key=week_key,
                prefix=prefix[:1],
                next_number=1,
            )
            session.add(sequence)
            session.flush()

        start = max(1, int(sequence.next_number or 1))
        # A partial/retried migration may already have created values for this
        # exact calendar date. Other years have distinct internal references.
        used = {
            str(value[0])
            for value in session.query(Order.business_order_number)
            .filter(Order.business_order_number.like(f"{prefix}-%"))
            .all()
            if value[0]
        }
        while f"{prefix}-{start}" in used or any(value.startswith(f"{prefix}-{start}-") for value in used):
            start += 1
        numbers = [f"{prefix}-{start}"]
        sequence.prefix = prefix[:1]  # Legacy one-character column; key owns the date.
        sequence.next_number = start + 1
        session.add(sequence)
        session.flush()
        return numbers, week_key

    def _ensure_performance_indexes(self):
        """为已有 SQLite 数据库补齐后台列表常用查询索引。"""
        if not self.engine.url.drivername.startswith("sqlite"):
            return
        statements = (
            "CREATE INDEX IF NOT EXISTS ix_users_active_created_at ON users (is_active, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_orders_user_id_status ON orders (user_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_orders_status_created_at ON orders (status, created_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS ix_orders_updated_at ON orders (updated_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS ix_orders_user_created_at ON orders (user_id, created_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS ix_orders_status_updated_at ON orders (status, updated_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS ix_order_items_order_id ON order_items (order_id)",
            "CREATE INDEX IF NOT EXISTS ix_fenghuodi_push_logs_order_status ON fenghuodi_push_logs (order_id, status, id)",
            "CREATE INDEX IF NOT EXISTS ix_products_parent_active ON products (parent_id, is_active, is_archived, default_warehouse_id)",
            "CREATE INDEX IF NOT EXISTS ix_warehouse_stock_warehouse_product ON warehouse_stock (warehouse_id, product_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_user_id ON payments (user_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_created_id ON payments (created_at, id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_user_created_id ON payments (user_id, created_at, id)",
            "CREATE INDEX IF NOT EXISTS ix_dropship_status_created_at ON dropship_auth_requests (status, created_at DESC, id DESC)",
        )
        with self.engine.begin() as connection:
            for statement in statements:
                # Upgrade snapshots can contain an older, deliberately
                # minimal table shape.  Index installation is an optimisation
                # and must never prevent the additive schema migrations that
                # follow from opening such a database.
                match = re.search(
                    r"CREATE INDEX IF NOT EXISTS \S+ ON (\S+) \(([^)]+)\)",
                    statement,
                    re.IGNORECASE,
                )
                if not match:
                    continue
                table_name = match.group(1)
                required = {
                    token.strip().split()[0]
                    for token in match.group(2).split(",")
                }
                columns = {
                    str(row[1])
                    for row in connection.exec_driver_sql(
                        f'PRAGMA table_info("{table_name}")'
                    ).fetchall()
                }
                if required.issubset(columns):
                    connection.exec_driver_sql(statement)

    def get_db(self) -> Session:
        """获取数据库会话"""
        db = self.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def create_user(self, telegram_id: str, username: Optional[str] = None, balance: float = 0.0) -> User:
        """创建用户"""
        with self.SessionLocal() as db:
            user = User(telegram_id=telegram_id, username=username, balance=balance)
            db.add(user)
            db.commit()
            db.refresh(user)
            return user

    def get_user(self, telegram_id: str) -> Optional[User]:
        """获取用户"""
        db = next(self.get_db())
        return db.query(User).filter(User.telegram_id == telegram_id).first()

    def update_user_balance(self, user_id: int, amount: float, transaction_type: str = "recharge",
                            description: str = None) -> bool:
        """更新用户余额"""
        db = next(self.get_db())
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False

        # 检查余额是否足够（消费时）
        if amount < 0 and user.balance < abs(amount):
            return False

        # 更新余额
        user.balance += amount

        # 创建交易记录
        transaction = Transaction(
            user_id=user_id,
            amount=amount,
            type=transaction_type,
            description=description
        )
        db.add(transaction)

        db.commit()
        return True

    def create_warehouse(self, name: str, address: str, shipping_fee: float = 0.0) -> Warehouse:
        """创建仓库"""
        db = next(self.get_db())
        warehouse = Warehouse(name=name, address=address, shipping_fee=shipping_fee)
        db.add(warehouse)
        db.commit()
        db.refresh(warehouse)
        return warehouse

    def get_warehouse(self, name: str) -> Optional[Warehouse]:
        """获取仓库"""
        db = next(self.get_db())
        return db.query(Warehouse).filter(Warehouse.name == name, Warehouse.is_active == True).first()

    def get_all_warehouses(self) -> List[Warehouse]:
        """获取所有仓库"""
        db = next(self.get_db())
        return db.query(Warehouse).filter(Warehouse.is_active == True).all()

    def create_product(self, name: str, price: float, description: str = None,
                       cost_price: float = None, level1_agent_price: float = None,
                       level2_agent_price: float = None,
                       default_warehouse_id: int = None) -> Product:
        """创建商品（兼容旧接口：price 作为 VIP 客户价）"""
        db = next(self.get_db())
        regular_price = price
        cost = cost_price if cost_price is not None else round(regular_price * 0.6, 2)
        lv1 = level1_agent_price if level1_agent_price is not None else round(regular_price * 0.8, 2)
        lv2 = level2_agent_price if level2_agent_price is not None else round(regular_price * 0.9, 2)
        product = Product(
            name=name,
            cost_price=cost,
            reference_price_state='SET' if cost_price is not None else 'UNSET',
            level1_agent_price=lv1,
            level2_agent_price=lv2,
            regular_price=regular_price,
            description=description,
            default_warehouse_id=default_warehouse_id,
            is_parent=False,
            is_active=True,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product

    def get_product(self, name: str) -> Optional[Product]:
        """获取商品"""
        db = next(self.get_db())
        return db.query(Product).filter(Product.name == name, Product.is_active == True).first()

    def update_warehouse_stock(self, warehouse_id: int, product_id: int, quantity: int) -> tuple[bool, Optional[str]]:
        """更新仓库库存"""
        db = next(self.get_db())

        # 检查仓库和商品是否存在
        warehouse = db.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
        if not warehouse:
            return False, "仓库不存在"

        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return False, "商品不存在"
        if product.default_warehouse_id != warehouse_id:
            return False, "商品只能在绑定仓库维护库存"

        stock = db.query(WarehouseStock).filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id
        ).first()

        if not stock:
            stock = WarehouseStock(warehouse_id=warehouse_id, product_id=product_id, quantity=max(0, quantity))
            db.add(stock)
        else:
            stock.quantity = max(0, quantity)

        self.refresh_product_stock_status(db, product_id)
        db.commit()
        return True, None

    def product_total_stock(self, session, product_id: int, warehouse_id: int = None) -> int:
        """获取商品所属发货仓库存，不跨仓库合并。"""
        if warehouse_id is None:
            product = session.query(Product).filter(Product.id == product_id).first()
            warehouse_id = product.default_warehouse_id if product else None
        if not warehouse_id:
            return 0
        stock = session.query(WarehouseStock).filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id,
        ).first()
        return int(stock.quantity or 0) if stock else 0

    def refresh_product_stock_status(self, session, product_id: int) -> Optional[Product]:
        """按商品绑定的默认发货仓库存刷新 TG 机器人停售状态。

        规则由 STOCK_AUTO_UNLIST_MODE 控制：
        - zero：默认发货仓库存 <= 0 自动停售。
        - threshold：默认发货仓库存 <= 商品预警值自动停售，预警值允许为 0。
        - 不修改 is_active，后台商品上架/下架只由管理员手动控制。
        """
        product = session.query(Product).filter(Product.id == product_id).first()
        if not product or product.is_parent:
            return product

        threshold = normalize_low_stock_threshold(product.low_stock_threshold)
        total_stock = self.product_total_stock(session, product_id, product.default_warehouse_id)
        auto_unlisted = should_auto_unlist(total_stock, threshold)
        if bool(product.auto_unlisted) != auto_unlisted:
            product.auto_unlisted = auto_unlisted
        return product

    def product_stock_pause_message(self, session, product: Product) -> str:
        threshold = normalize_low_stock_threshold(getattr(product, "low_stock_threshold", None))
        warehouse_id = product.default_warehouse_id
        total_stock = self.product_total_stock(session, product.id, warehouse_id)
        warehouse = session.query(Warehouse).filter(Warehouse.id == warehouse_id).first() if warehouse_id else None
        warehouse_label = warehouse.name if warehouse else "未绑定发货仓"
        if stock_auto_unlist_mode() == "zero":
            return (
                f"商品库存为 0，TG机器人暂不出货: {product.get_full_name()}"
                f"（{warehouse_label}当前库存 {total_stock}）"
            )
        return (
            f"商品已到达预警库存，TG机器人暂不出货: {product.get_full_name()}"
            f"（{warehouse_label}当前库存 {total_stock}，预警 {threshold}）"
        )

    def get_warehouse_stock(self, warehouse_id: int, product_id: int) -> int:
        """获取仓库中商品的库存"""
        db = next(self.get_db())
        stock = db.query(WarehouseStock).filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id
        ).first()
        return stock.quantity if stock else 0

    def get_all_stock(self) -> List[Dict]:
        """获取所有仓库和商品的库存"""
        db = next(self.get_db())

        stocks = db.query(WarehouseStock, Warehouse, Product).join(
            Warehouse, WarehouseStock.warehouse_id == Warehouse.id
        ).join(
            Product, WarehouseStock.product_id == Product.id
        ).filter(
            Warehouse.is_active == True,
            Product.is_active == True,
            Product.default_warehouse_id == WarehouseStock.warehouse_id,
        ).all()

        result = []
        for stock, warehouse, product in stocks:
            result.append({
                "warehouse_id": warehouse.id,
                "warehouse_name": warehouse.name,
                "product_id": product.id,
                "product_name": product.name,
                "price": product.price,
                "quantity": stock.quantity,
                "last_updated": stock.last_updated
            })

        return result

    def check_warehouse_stock(self, warehouse_id: int, product_id: int, required_quantity: int) -> tuple[bool, Optional[str]]:
        """
        检查仓库库存是否充足
        Returns: (是否充足, 不足时的商品信息或None)
        """
        db = next(self.get_db())
        stock = db.query(WarehouseStock).filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id
        ).first()

        if not stock:
            return False, "商品在指定仓库没有库存"

        if stock.quantity < required_quantity:
            product = db.query(Product).filter(Product.id == product_id).first()
            return False, f"{product.name} 在 {stock.warehouse.name} 库存不足（当前: {stock.quantity}, 需要: {required_quantity}）"

        return True, None

    def calculate_shipping_fees(self, items: List[Dict]) -> tuple[float, List[Dict]]:
        """
        计算运费
        默认按数量规则：普通与烟弹可分别设置每段数量和每段运费。
        可通过 SHIPPING_MODE=warehouse 回退到仓库运费。
        """
        from config import SHIPPING_MODE
        db = next(self.get_db())
        items_with_fee = []

        if SHIPPING_MODE != 'warehouse':
            rows = []
            for item in items:
                product = None
                if item.get('product_id'):
                    product = db.query(Product).filter(Product.id == item['product_id']).first()
                rows.append({
                    'product': product,
                    'product_name': item.get('product_name') or (product.name if product else ''),
                    'quantity': item.get('quantity') or 0,
                })
            calc = self.calc_qty_shipping_fee(rows)
            total_shipping_fee = calc['total_shipping_fee']
            # 运费挂在第一件上，便于展示
            first = True
            for item in items:
                row = item.copy()
                row['shipping_fee'] = total_shipping_fee if first else 0
                first = False
                items_with_fee.append(row)
            return total_shipping_fee, items_with_fee

        # 兼容旧：按仓库计运费
        warehouses_used = set()
        total_shipping_fee = 0
        for item in items:
            warehouse_id = item.get('warehouse_id')
            warehouse = db.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
            row = item.copy()
            if warehouse and warehouse.id not in warehouses_used:
                warehouses_used.add(warehouse.id)
                total_shipping_fee += warehouse.shipping_fee
                row['shipping_fee'] = warehouse.shipping_fee
            else:
                row['shipping_fee'] = 0
            items_with_fee.append(row)
        return total_shipping_fee, items_with_fee

    def create_order(self, user_id: int, items: list, recipient_name: str,
                     recipient_phone: str, recipient_address: str, notes: str = None,
                     source_chat_id: str = None, source_message_id: int = None,
                     source_chat_type: str = None, source_chat_title: str = None,
                     split_units: int = None, auto_allocate: bool = False,
                     expected_quote: Optional[dict] = None,
                     qualification_required: bool = False,
                     single_order: bool = False, automatic_order: bool = False,
                     cross_warehouse_confirmed: bool = False,
                     explicit_warehouse_id: Optional[int] = None,
                     bundle_requests: Optional[list] = None,
                     execution_info: Optional[dict] = None,
                     proxy_info: Optional[dict] = None) -> Optional[Order]:
        """创建订单；超过 ``split_units`` 时在同一事务中生成正式子单。

        返回第一笔订单以兼容旧调用；完整结果放在运行时属性
        ``created_orders`` 中。正式分单共享业务基础编号并追加 -1/-2/-3。
        """
        import package_rules
        from config import CATEGORY_SPLIT_LIMITS, ORDER_AUTO_SPLIT_UNITS, SHIPPING_MODE

        session = next(self.get_db())
        try:
            from order_trace import stage
            from address_source_guard import validate_persisted_region,validate_current_region
            from source_coverage import validate_persisted_source_total
            if source_chat_id is not None and source_message_id is not None:
                # Pure CPU evidence checks do not need the SQLite writer.
                # The original checks below reread source/catalog under the
                # writer; request memo reuse requires identical evidence.
                with stage('SOURCE_PREVALIDATION'), self.SessionLocal() as evidence:
                    from execution_safety import check_submit
                    check_submit(evidence,execution_info,source_chat_id,source_message_id)
                    validate_current_region(recipient_address)
                    validate_persisted_region(evidence,source_chat_id,source_message_id,recipient_address)
                    validate_persisted_source_total(evidence,source_chat_id,source_message_id,items,bundle_requests=bundle_requests)
            import config as admission_settings
            if int(getattr(admission_settings,'ADDRESS_DAILY_MAX_ORDERS',0) or 0):
                # Normalize existing addresses before taking the writer. Every
                # admission/count/status is still reread under BEGIN IMMEDIATE.
                try:
                    from address_daily_orders import prewarm
                    with stage('ADDRESS_QUOTA_PREWARM'):
                        session.info['address_daily_normalization']=prewarm(
                            self,datetime.utcnow(),extra_addresses=(recipient_address,))
                except Exception:
                    # Optional CPU warmup is never permission to skip quota
                    # checks; a miss falls back to the original in-txn path.
                    pass
            with stage('DB_CONNECTION_WAIT'):
                connection=session.connection()
            with stage('ORDER_WRITE_LOCK_WAIT'):
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            unit_values=dict(session.connection().exec_driver_sql('SELECT product_id,unit FROM product_units').all())
            pending_unit_snapshots=[]
            from execution_safety import check_submit, record_commits
            check_submit(session,execution_info,source_chat_id,source_message_id)
            proxy_draft=None
            if proxy_info is not None:
                from proxy_confirmation import validate_submit
                proxy_draft=validate_submit(session,proxy_info,user_id,source_chat_id,source_message_id,
                    expected_quote,(recipient_name,recipient_phone,recipient_address))
            if qualification_required:
                import telegram_accounts
                account = session.query(User).filter(User.id == user_id).first()
                if not account or not telegram_accounts.eligible(session, account):
                    raise ValueError('QUALIFICATION_REQUIRED:当前账户未通过下单审核')
                if source_chat_id is not None and source_message_id is not None:
                    # The sender account is authoritative; historic bindings
                    # are not an order gate. Reuse the durable message fact.
                    from sqlalchemy import text
                    from telegram_source_identity import parameters
                    fact=session.execute(text('SELECT user_id,sender_id FROM telegram_message_events WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'),parameters(source_chat_id,message=source_message_id)).mappings().first()
                    if fact and (fact['user_id']!=user_id or str(fact['sender_id'])!=str(account.telegram_id)):
                        raise ValueError('QUALIFICATION_REQUIRED:订单消息发送人与当前账户不一致')
                    if not fact:
                        if not telegram_accounts.claim_message(session,source_chat_id,source_message_id,account.telegram_id,user_id):
                            raise ValueError('MESSAGE_SOURCE_UNCONFIRMED:历史消息来源未确认，禁止继续建单')
            blocked, hit = self.is_address_blocked(recipient_address, session=session)
            if blocked:
                from blocked_regions import rejection
                raise ValueError('BLOCKED_ADDRESS:' + rejection(hit))

            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return None
            if source_chat_id is not None and source_message_id is not None:
                from sqlalchemy import inspect,text
                if inspect(session.connection()).has_table('telegram_order_jobs'):
                    from telegram_source_identity import parameters
                    cancelled=session.execute(text("SELECT 1 FROM telegram_order_jobs WHERE namespace IN (:namespace,'legacy:unknown') AND chat_id=:chat AND message_id=:message AND state='CANCELLED_TEST'"),parameters(source_chat_id,message=source_message_id)).scalar()
                    if cancelled:raise ValueError('TEST_MESSAGE_CANCELLED:该测试消息已由人工终结，禁止继续建单')
                # The surrounding BEGIN IMMEDIATE serializes this lookup and
                # creation. A repeated group confirmation never reallocates or
                # consumes stock/FIFO, including after a Bot restart.
                previous=session.query(Order).filter(
                    Order.source_chat_id==str(source_chat_id),
                    Order.source_message_id==source_message_id)
                from telegram_source_identity import filter_current_source_orders
                previous=filter_current_source_orders(previous,source_chat_id).order_by(Order.id).all()
                if previous:
                    if any(row.user_id!=user_id for row in previous):
                        raise ValueError('群消息已经属于其他账户，禁止重复或跨账户处理')
                    raise ValueError('MESSAGE_ALREADY_PROCESSED:该群消息已经建单，禁止重复处理')
            if not items and not bundle_requests:
                raise ValueError("订单商品不能为空")
            # Ordinary requested items only; bundle composition is unchanged.
            # BEGIN IMMEDIATE also serializes this check with limit edits.
            from product_order_limits import validate as validate_product_limits
            from order_trace import stage
            with stage('PRODUCT_LIMIT_GUARD'):
                validate_product_limits(session,items)
            from address_source_guard import validate_persisted_region,validate_current_region
            with stage('REGION_SOURCE_GUARD'):
                validate_current_region(recipient_address)
                validate_persisted_region(session,source_chat_id,source_message_id,recipient_address)
            from source_coverage import validate_persisted_source_total
            with stage('QUANTITY_SOURCE_GUARD'):
                validate_persisted_source_total(session,source_chat_id,source_message_id,items,bundle_requests=bundle_requests)
            allocation = None
            if auto_allocate and items and not bundle_requests:
                from warehouse_allocation import select_warehouse
                from order_trace import stage
                with stage('WAREHOUSE_ASSIGNMENT'):
                    selected, mode, audit = select_warehouse(session, items, user,
                        cross_warehouse_confirmed=cross_warehouse_confirmed,
                        explicit_warehouse_id=explicit_warehouse_id)
                items[:] = selected
                allocation = (mode, audit)

            bundle_sales = {}
            independent_rows=set()
            if bundle_requests:
                from bundles import full_plan
                ordinary, expanded, bundle_sales = full_plan(session,bundle_requests,user,items,
                    explicit_warehouse_id=explicit_warehouse_id)
                items = ordinary + expanded
                independent_rows={id(row) for row in expanded if row.get('independent_bundle_id') is not None}
                if len({row['warehouse_id'] for row in items})>1 and not cross_warehouse_confirmed:
                    from warehouse_allocation import CrossWarehouseRequired
                    raise CrossWarehouseRequired('当前订单产品存在于不同仓库，是否分单发货？')

            prepared = []
            stock_needs = {}
            stocks = {}
            # Transaction-local snapshots: never cache prices across orders or
            # principals. BEGIN IMMEDIATE keeps concurrent price edits outside
            # this order's final pricing/stock commit window.
            flavor_prices = dict(session.query(UserProductPrice.product_id, UserProductPrice.unit_price)
                                 .filter(UserProductPrice.user_id == user_id).all())
            category_prices = dict(session.query(UserCategoryPrice.parent_id, UserCategoryPrice.unit_price)
                                   .filter(UserCategoryPrice.user_id == user_id).all())
            for raw_item in items:
                quantity = int(raw_item.get('quantity') or 0)
                if quantity <= 0:
                    raise ValueError("商品数量必须大于0")
                product = session.query(Product).filter(
                    Product.id == int(raw_item['product_id']),
                    Product.is_active == True,
                ).first()
                if not product:
                    return None
                warehouse_id = int(raw_item['warehouse_id'])
                if int(product.default_warehouse_id or 0) != warehouse_id:
                    raise ValueError(f"商品只能从绑定仓库发货: {product.get_full_name()}")
                warehouse = session.query(Warehouse).filter(
                    Warehouse.id == warehouse_id,
                    Warehouse.is_active == True,
                ).first()
                if not warehouse:
                    raise ValueError("发货仓库不存在或已停用")

                independent=raw_item.get('independent_bundle_id') is not None
                if independent and id(raw_item) not in independent_rows:
                    raise ValueError('独立福袋库存标识必须来自事务内有效福袋计划')
                if not independent:
                    self.refresh_product_stock_status(session, product.id)
                    session.refresh(product)
                if not product.is_active:
                    raise ValueError(f"商品已下架，不能下单: {product.get_full_name()}")
                if product.parent_id:
                    parent=session.get(Product,product.parent_id)
                    if not parent or parent.is_archived or not parent.is_active:
                        raise ValueError('商品分类已暂停接单，不能下单')
                if not independent and bool(getattr(product, "auto_unlisted", False)):
                    raise ValueError(self.product_stock_pause_message(session, product))

                stock = session.query(WarehouseStock).filter(
                    WarehouseStock.warehouse_id == warehouse_id,
                    WarehouseStock.product_id == product.id,
                ).first()
                key = (warehouse_id, product.id)
                if not independent:
                    stocks[key] = stock
                    stock_needs[key] = stock_needs.get(key, 0) + quantity

                from order_trace import stage
                with stage('PRICE'):
                    custom_price = flavor_prices.get(product.id)
                    category_price = category_prices.get(product.parent_id)
                    selling_price = (
                        __import__('dynamic_prices').custom(product,custom_price)
                        if custom_price is not None
                        else __import__('dynamic_prices').custom(product,category_price) if category_price is not None
                        else float(product.get_price_for_agent_level(user.agent_level))
                    )
                prepared.append({
                    'product_id': product.id,
                    'product': product,
                    'warehouse_id': warehouse_id,
                    'warehouse': warehouse,
                    'quantity': quantity,
                    'unit_price': raw_item['bundle_unit_price'] if raw_item.get('bundle_instance') is not None else selling_price,
                    'unit':unit_values.get(product.id,unit_values.get(product.parent_id,'支')),
                    **({'bundle_instance':raw_item['bundle_instance']} if raw_item.get('bundle_instance') is not None else {}),
                    **({'independent_bundle_id':raw_item['independent_bundle_id']} if independent else {}),
                    'current_stock': int(stock.quantity or 0) if stock else 0,
                })

            from order_trace import stage
            with stage('STOCK_VALIDATION'):
                for key, required in stock_needs.items():
                    stock = stocks.get(key)
                    if not stock or int(stock.quantity or 0) < required:
                        return None

            # Final allocation and accepted money are validated in this same
            # transaction BEFORE FIFO, stock, order numbers or orders are changed.
            capacity = int(split_units or ORDER_AUTO_SPLIT_UNITS or 6)
            ordinary_prepared = [row for row in prepared if row.get('bundle_instance') is None]
            if automatic_order:
                from parcel_shipping import plan as parcel_plan
                packages=parcel_plan(ordinary_prepared)
            else:
                packages = ([ordinary_prepared] if single_order and ordinary_prepared else
                    package_rules.split_dict_items_by_category(ordinary_prepared, capacity, CATEGORY_SPLIT_LIMITS))
            if bundle_requests:
                from bundles import packages as bundle_packages
                from sqlalchemy import text
                bundle_limit=session.execute(text('SELECT max_portions FROM bundle_settings WHERE id=1')).scalar()
                packages=bundle_packages(prepared,bundle_sales,packages,bundle_limit)
            if not packages:
                raise ValueError('订单商品数量必须大于0')
            from order_result import quote_packages, monetary_terms, ReconfirmationRequired
            from order_trace import stage
            with stage('PRICE_AND_SHIPPING'):
                final_quote, package_quotes = quote_packages(self, packages, SHIPPING_MODE)
            if expected_quote is not None and monetary_terms(expected_quote) != monetary_terms(final_quote):
                raise ReconfirmationRequired(final_quote)

            # 一次性消费完整订单的 FIFO 批次，再按平均成本分摊至各子单。
            bundle_cost_pools = {}
            if bundle_requests:
                # Several sales portions may consume the same real SKU. Consume
                # it ONCE, before changing physical stock, then attribute lots.
                from inventory_costs import take_details
                for (warehouse_id,product_id),quantity in stock_needs.items():
                    _,_,details=self.consume_batches_fifo(session,product_id,warehouse_id,quantity)
                    bundle_cost_pools[(product_id,warehouse_id)]=details
            for item in prepared:
                with stage('FIFO'):
                    if item.get('independent_bundle_id') is not None:
                        from bundle_inventory import consume
                        sale=bundle_sales[item['bundle_instance']]
                        if 'inventory_movement_id' not in sale:
                            import uuid
                            movement=consume(session,sale['bundle_id'],1,key='order:'+uuid.uuid4().hex,
                                actor='order-user:'+str(user_id))
                            sale['inventory_movement_id']=movement['id']
                            per,remainder=divmod(movement['cost_cents'],sale['snapshot']['config']['units'])
                            sale['_cost_units']=[(per+int(i<remainder))/100 for i in range(sale['snapshot']['config']['units'])]
                        avg_cost=sale['_cost_units'].pop(0)
                        batch_details=[dict(qty=1,unit_cost=avg_cost,cost_source='BUNDLE_INDEPENDENT',
                            bundle_movement_id=sale['inventory_movement_id'])]
                    elif bundle_requests:
                        batch_details=take_details(bundle_cost_pools[(item['product_id'],item['warehouse_id'])],item['quantity'])
                        avg_cost=sum(d['qty']*float(d.get('unit_cost') or 0) for d in batch_details)/item['quantity']
                    else:
                        avg_cost, _cost_amount, batch_details = self.consume_batches_fifo(
                            session,
                            item['product_id'],
                            item['warehouse_id'],
                            item['quantity'],
                        )
                item['unit_cost'] = float(avg_cost or 0)
                item['batch_details'] = batch_details

            import json
            from inventory_costs import take_details, is_unknown, INCOMPLETE
            cost_pools = {}
            def pool_key(row):
                return (row['product_id'],row['warehouse_id'],row.get('bundle_instance') if row.get('independent_bundle_id') is not None else None)
            for item in prepared:
                cost_pools.setdefault(pool_key(item),[]).extend(
                    dict(d) for d in item['batch_details'])

            split_count = len(packages)
            base_numbers, week_key = self._allocate_order_numbers(
                session,
                split_count,
            )
            base_number = base_numbers[0]
            public_order_numbers = (
                [f"{base_number}-{index}" for index in range(1, split_count + 1)]
                if split_count > 1 else [base_number]
            )
            # This reference remains globally unique across weekly resets and
            # is never shown as the business order number.
            order_numbers = [f"{week_key}-{value}" for value in public_order_numbers]
            split_group_id = (
                f"{week_key}-{base_number}"
                if split_count > 1 else None
            )
            created_orders = []

            for package_index, package in enumerate(packages, start=1):
                total_amount = sum(
                    float(item['unit_price']) * int(item['quantity']) for item in package
                )
                total_profit = sum(
                    (float(item['unit_price']) - float(item.get('unit_cost') or 0))
                    * int(item['quantity'])
                    for item in package
                )

                total_shipping_fee, rows_with_fee = package_quotes[package_index - 1]

                for row in rows_with_fee:
                    row['cost_details']=take_details(cost_pools[pool_key(row)],int(row['quantity']))
                    row['cost_status']=INCOMPLETE if is_unknown(row['cost_details']) else 'COST_COMPLETE'
                    row['unit_cost']=0.0 if row['cost_status']==INCOMPLETE else sum(
                        d['qty']*d['unit_cost'] for d in row['cost_details'])/int(row['quantity'])
                cost_status=INCOMPLETE if any(row['cost_status']==INCOMPLETE for row in rows_with_fee) else 'COST_COMPLETE'
                # Existing NOT NULL monetary columns use 0 only as a legacy storage
                # placeholder. Cost status is authoritative; APIs/stats expose null.
                total_profit=0.0 if cost_status==INCOMPLETE else sum(
                    (row['unit_price']-row['unit_cost'])*int(row['quantity']) for row in rows_with_fee)
                order_number = order_numbers[package_index - 1]
                order = Order(
                    user_id=user_id,
                    order_number=order_number,
                    business_order_number=public_order_numbers[package_index - 1],
                    total_amount=total_amount,
                    total_shipping_fee=total_shipping_fee,
                    grand_total=total_amount + total_shipping_fee,
                    total_profit=total_profit,
                    cost_status=cost_status,
                    paid_amount=0.0,
                    payment_status='unpaid',
                    status='pending' if automatic_order or split_count == 1 else 'split_pending',
                    split_group_id=split_group_id,
                    split_index=package_index if split_count > 1 else None,
                    split_count=split_count,
                    recipient_name=recipient_name,
                    recipient_phone=recipient_phone,
                    recipient_address=recipient_address,
                    notes=notes,
                    source_chat_id=str(source_chat_id) if source_chat_id is not None else None,
                    source_message_id=source_message_id,
                    source_chat_type=source_chat_type,
                    source_chat_title=source_chat_title,
                )
                session.add(order)
                session.flush()
                if bundle_requests and not allocation:
                    from warehouse_allocation import record
                    record(session,order.id,rows_with_fee[0]['warehouse_id'],'福袋单仓完整履约',
                        dict(bundle_ids=sorted({bundle_sales[row['bundle_instance']]['bundle_id'] for row in rows_with_fee if row.get('bundle_instance') is not None}),
                            selected_warehouse_id=rows_with_fee[0]['warehouse_id']))
                if allocation:
                    from warehouse_allocation import record
                    record(session,order.id,rows_with_fee[0]['warehouse_id'],*allocation)

                for item_data in rows_with_fee:
                    quantity = int(item_data['quantity'])
                    unit_cost = float(item_data.get('unit_cost') or 0)
                    order_item = OrderItem(
                        order_id=order.id,
                        product_id=item_data['product_id'],
                        warehouse_id=item_data['warehouse_id'],
                        quantity=quantity,
                        unit_price=float(item_data['unit_price']),
                        unit_cost=unit_cost,
                        cost_amount=unit_cost * quantity,
                        shipping_fee=float(item_data.get('shipping_fee') or 0),
                        profit=0.0 if item_data['cost_status']==INCOMPLETE else (float(item_data['unit_price']) - unit_cost) * quantity,
                        cost_status=item_data['cost_status'],
                        unknown_cost_quantity=sum(d['qty'] for d in item_data['cost_details'] if d['unit_cost'] is None),
                        fifo_details=json.dumps(item_data['cost_details']),
                    )
                    session.add(order_item)
                    unit_product=item_data['product']
                    pending_unit_snapshots.append((order_item,unit_values.get(unit_product.id,unit_values.get(unit_product.parent_id,'支'))))
                    if item_data.get('bundle_instance') is not None:
                        session.flush()
                        item_data['persisted_item_id']=order_item.id
                    if item_data.get('independent_bundle_id') is not None:
                        continue
                    stock = stocks[(item_data['warehouse_id'], item_data['product_id'])]
                    stock.quantity -= quantity
                    session.add(StockMovement(
                        product_id=item_data['product_id'],
                        warehouse_id=item_data['warehouse_id'],
                        change_qty=-quantity,
                        movement_type='order_out',
                        ref_type='order',
                        ref_id=order_number,
                        note=(
                            f'自动分单出库 {package_index}/{split_count}'
                            if split_count > 1 else '下单出库'
                        ),
                    ))
                if bundle_requests:
                    from bundles import save_sales
                    save_sales(session,order,rows_with_fee,bundle_sales)
                created_orders.append(order)

            for product_id in {item['product_id'] for item in prepared if item.get('independent_bundle_id') is None}:
                self.refresh_product_stock_status(session, product_id)
            from split_management import attach_created
            session.flush()  # Include this transaction's newly added item quantities.
            if pending_unit_snapshots:
                session.connection().exec_driver_sql('INSERT INTO order_item_units(item_id,unit) VALUES(?,?)',
                    [(item.id,unit) for item,unit in pending_unit_snapshots])
            with stage('ADDRESS_DAILY_ADMISSION'):
                attach_created(session, created_orders)
            remaining_stock = [dict(product_id=product_id, warehouse_id=warehouse_id,
                product_name=stock.product.name, warehouse_name=stock.warehouse.name,
                quantity_ordered=stock_needs[(warehouse_id,product_id)],
                remaining_quantity=int(stock.quantity))
                for (warehouse_id,product_id),stock in stocks.items()]
            check_submit(session,execution_info,source_chat_id,source_message_id)
            record_commits(session,execution_info,created_orders)
            if proxy_draft is not None:
                from proxy_confirmation import finish
                finish(session,proxy_info,proxy_draft,created_orders[0])
            from order_trace import commit_clock,committed
            clocks=commit_clock(session,execution_info)
            with stage('ORDER_COMMIT'):
                session.commit()
            committed(clocks)
            for order in created_orders:
                session.refresh(order)
            first_order = created_orders[0]
            first_order.created_orders = created_orders
            first_order.final_remaining_stock = remaining_stock
            return first_order
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_orders(self, user_id: int = None, status: str = None, limit: int = 100):
        """获取订单列表"""
        db = next(self.get_db())
        query = db.query(Order)

        if user_id:
            query = query.filter(Order.user_id == user_id)

        if status:
            query = query.filter(Order.status == status)

        query = query.order_by(Order.created_at.desc())

        if limit:
            query = query.limit(limit)

        return query.all()

    def get_order_by_number(self, order_number: str):
        """根据订单号获取订单"""
        db = next(self.get_db())
        order_number = (order_number or '').strip().lstrip('#')
        return db.query(Order).filter(Order.order_number == order_number).first()

    def request_order_cancellation(self, order_id: int = None, order_number: str = None,
                                   telegram_id: str = None, reason: str = None):
        """创建客户取消申请，不改变订单状态、不回滚库存。"""
        db = next(self.get_db())
        try:
            query = db.query(Order)
            if order_id is not None:
                order = query.filter(Order.id == order_id).first()
            elif order_number:
                clean_number = str(order_number).strip().lstrip('#')
                order = query.filter(Order.order_number == clean_number).first()
            else:
                return False, "缺少订单标识"
            if not order:
                return False, "订单不存在"
            if telegram_id:
                user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
                if not user or order.user_id != user.id:
                    return False, "只能申请取消自己的订单"
            if order.status == 'cancelled':
                return False, "订单已取消，无需重复申请"
            if order.status not in ('pending', 'confirmed', 'shipped'):
                return False, f"当前状态不可申请取消: {order.status}"

            existing = (
                db.query(OrderCancellationRequest)
                .filter(
                    OrderCancellationRequest.order_id == order.id,
                    OrderCancellationRequest.status == 'pending',
                )
                .order_by(OrderCancellationRequest.id.desc())
                .first()
            )
            if existing:
                return True, {
                    'request_id': existing.id, 'order_id': order.id,
                    'order_number': order.order_number, 'status': existing.status,
                    'reason': existing.reason, 'created_at': existing.created_at,
                    'duplicate': True,
                }

            requester_id = str(telegram_id or (order.user.telegram_id if order.user else ''))
            request = OrderCancellationRequest(
                order_id=order.id, user_id=order.user_id, telegram_id=requester_id,
                order_number=order.order_number,
                reason=(reason or '用户主动申请取消').strip()[:1000], status='pending',
            )
            db.add(request)
            db.commit()
            db.refresh(request)
            return True, {
                'request_id': request.id, 'order_id': order.id,
                'order_number': order.order_number, 'status': request.status,
                'reason': request.reason, 'created_at': request.created_at,
                'duplicate': False,
            }
        except Exception as e:
            db.rollback()
            return False, str(e)
        finally:
            db.close()

    def review_order_cancellation(self, request_id: int, decision: str,
                                  admin_note: str = None, reviewed_by: str = None):
        """审核取消申请；批准时调用既有取消逻辑，保证库存/余额回滚口径一致。"""
        decision = str(decision or '').strip().lower()
        if decision not in ('approve', 'reject'):
            return False, "审核决定必须是 approve 或 reject"
        db = next(self.get_db())
        try:
            request = db.query(OrderCancellationRequest).filter(
                OrderCancellationRequest.id == request_id,
            ).first()
            if not request:
                return False, "取消申请不存在"
            if request.status != 'pending':
                return False, f"取消申请已处理: {request.status}"
            if decision == 'reject':
                request.status = 'rejected'
                request.admin_note = (admin_note or '').strip()[:1000] or None
                request.reviewed_by = (reviewed_by or '').strip()[:100] or None
                request.reviewed_at = datetime.utcnow()
                db.commit()
                return True, {
                    'request_id': request.id, 'order_id': request.order_id,
                    'order_number': request.order_number, 'status': request.status,
                }
            order_id, order_number, reason = request.order_id, request.order_number, request.reason
        finally:
            db.close()

        success, result = self.cancel_order(
            order_id=order_id, reason=(admin_note or '').strip() or reason or '后台批准客户取消申请', allow_shipped=True,
        )
        if not success:
            return False, result
        db = next(self.get_db())
        try:
            request = db.query(OrderCancellationRequest).filter(
                OrderCancellationRequest.id == request_id,
            ).first()
            if not request:
                return False, "取消申请已消失，订单已取消"
            request.status = 'approved'
            request.admin_note = (admin_note or '').strip()[:1000] or None
            request.reviewed_by = (reviewed_by or '').strip()[:100] or None
            request.reviewed_at = datetime.utcnow()
            db.commit()
            return True, {
                'request_id': request.id, 'order_id': order_id,
                'order_number': order_number, 'status': request.status,
                'cancel_result': result,
            }
        except Exception as e:
            db.rollback()
            return False, str(e)
        finally:
            db.close()

    def cancel_order(self, order_id: int = None, order_number: str = None,
                     reason: str = None, allow_shipped: bool = True,
                     _cancel_group: bool = False):
        """取消订单并回滚库存/余额。

        Returns: (success, result_or_error)
        result: {
          order_id, order_number, refund_amount, restored_stock: [...], user_balance
        }
        """
        db = next(self.get_db())
        try:
            if self.engine.dialect.name == 'sqlite':
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            query = db.query(Order)
            if order_id is not None:
                order = query.filter(Order.id == order_id).first()
            elif order_number:
                order_number = order_number.strip().lstrip('#')
                order = query.filter(Order.order_number == order_number).first()
            else:
                return False, "缺少订单标识"

            if not order:
                return False, "订单不存在"

            if order.status == 'cancelled':
                return False, "订单已取消，无需重复操作"

            if order.status == 'shipped' and not allow_shipped:
                return False, "已发货订单不能直接取消，请先联系管理员处理"

            if order.status not in ('pending', 'confirmed', 'shipped', 'split_pending'):
                return False, f"当前状态不可取消: {order.status}"

            restored_stock = []
            import json
            from inventory_costs import resolve_details, BLOCKED
            from bundle_inventory import order_links, restore as restore_bundle
            bundle_links,independent_items=order_links(db,order.id)
            restorations={}
            # Validate every cost source before changing stock/balance/status.
            for item in order.items:
                if item.id in independent_items:
                    continue
                if item.fifo_details and item.fifo_details!='[]':
                    try:
                        details=resolve_details(db,json.loads(item.fifo_details))
                        if sum(d['qty'] for d in details)!=item.quantity:
                            return False,BLOCKED
                        restorations[item.id]=details
                    except ValueError:
                        return False,BLOCKED
                elif item.cost_status!='COST_COMPLETE':
                    return False,BLOCKED
            # 回滚库存
            for sale_id,movement_id in bundle_links.items():
                restore_bundle(db,movement_id,key='cancel-bundle-sale:'+str(sale_id),actor='cancel-order:'+str(order.id))
            for item in order.items:
                if item.id in independent_items:
                    continue
                stock = db.query(WarehouseStock).filter(
                    WarehouseStock.warehouse_id == item.warehouse_id,
                    WarehouseStock.product_id == item.product_id
                ).first()
                if not stock:
                    stock = WarehouseStock(
                        warehouse_id=item.warehouse_id,
                        product_id=item.product_id,
                        quantity=0
                    )
                    db.add(stock)
                    db.flush()

                old_qty = stock.quantity
                stock.quantity = old_qty + item.quantity
                # Restore original proven FIFO layers, never fabricate historical lots.
                if item.id in restorations:
                    for detail in restorations[item.id]:
                        lot=db.get(InventoryBatch,detail['batch_id'])
                        if not lot or lot.remain_qty+detail['qty']>lot.quantity:
                            db.rollback(); return False,BLOCKED
                        lot.remain_qty+=detail['qty']
                else:
                    self.restore_batches_on_cancel(
                        db,
                        item.product_id,
                        item.warehouse_id,
                        item.quantity,
                        unit_cost=float(getattr(item, 'unit_cost', 0) or 0),
                    )
                restored_stock.append({
                    'product_id': item.product_id,
                    'product_name': item.product.name if item.product else '',
                    'warehouse_id': item.warehouse_id,
                    'warehouse_name': item.warehouse.name if item.warehouse else '',
                    'quantity_restored': item.quantity,
                    'old_quantity': old_qty,
                    'new_quantity': stock.quantity,
                })
                self.refresh_product_stock_status(db, item.product_id)

            # 人工记账模式：取消订单 = 取消欠款；若已收部分款项，退回预存或记负向收账由运营处理
            user = db.query(User).filter(User.id == order.user_id).first()
            already_paid = order.paid_amount or 0
            refund_amount = already_paid
            if user and already_paid > 0:
                # 已收款部分退回预存，便于后续再结
                user.balance += already_paid
                desc = f"取消订单退回已收款 #{order.order_number}"
                if reason:
                    desc += f"，原因: {reason}"
                db.add(Transaction(
                    user_id=user.id,
                    amount=already_paid,
                    type='refund',
                    description=desc,
                ))

            # 回滚库存流水
            for item in order.items:
                if item.id in independent_items:
                    continue
                db.add(StockMovement(
                    product_id=item.product_id,
                    warehouse_id=item.warehouse_id,
                    change_qty=item.quantity,
                    movement_type='cancel_in',
                    ref_type='order',
                    ref_id=order.order_number,
                    note='取消订单回滚库存'
                ))

            order.status = 'cancelled'
            order.payment_status = 'paid' if already_paid <= 0 else 'partial'
            # 取消后未结金额视为 0
            order.paid_amount = order.grand_total
            if reason:
                note = order.notes or ''
                extra = f'[取消] {reason}'
                order.notes = (note + chr(10) + extra).strip() if note else extra

            from cancellation_facts import record as record_cancellation
            record_cancellation(db, order, str(reason or '').strip(), user)
            from address_daily_orders import release_cancelled
            released_order_ids=release_cancelled(db,order,datetime.utcnow())

            db.commit()
            db.refresh(order)
            if user:
                db.refresh(user)

            result = {
                'order_id': order.id,
                'order_number': order.order_number,
                'refund_amount': refund_amount,
                'restored_stock': restored_stock,
                'user_balance': user.balance if user else None,
                'status': order.status,
                'cancelled_order_ids': [order.id],
                'cancelled_order_numbers': [order.order_number],
                'cancelled_count': 1,
                'released_order_ids': released_order_ids,
            }
            return True, result
        except Exception as e:
            db.rollback()
            return False, str(e)
        finally:
            db.close()



    def restore_order(self, order_id: int = None, order_number: str = None,
                      to_status: str = "pending", reason: str = None):
        """Restore a cancelled order: re-deduct stock and reverse refund if needed."""
        if to_status not in ("pending", "confirmed"):
            return False, "\u6062\u590d\u540e\u72b6\u6001\u4ec5\u652f\u6301\u5f85\u5904\u7406\u6216\u5df2\u786e\u8ba4"

        db = self.SessionLocal()
        try:
            if self.engine.dialect.name == 'sqlite':
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            query = db.query(Order)
            if order_id is not None:
                order = query.filter(Order.id == order_id).first()
            elif order_number:
                order_number = order_number.strip().lstrip("#")
                order = query.filter(Order.order_number == order_number).first()
            else:
                return False, "\u7f3a\u5c11\u8ba2\u5355\u6807\u8bc6"

            if not order:
                return False, "\u8ba2\u5355\u4e0d\u5b58\u5728"
            if order.status != "cancelled":
                return False, f"\u53ea\u80fd\u6062\u590d\u5df2\u53d6\u6d88\u8ba2\u5355\uff0c\u5f53\u524d\u72b6\u6001: {order.status}"
            from bundle_inventory import order_links
            if order_links(db,order.id)[0]:
                return False,'独立库存福袋取消单暂不支持恢复原单，请重新下单；库存和账务未改变'

            refund_tx = None
            if order.user_id:
                refund_tx = (
                    db.query(Transaction)
                    .filter(
                        Transaction.user_id == order.user_id,
                        Transaction.type == "refund",
                        Transaction.description.like(f"%#{order.order_number}%"),
                    )
                    .order_by(Transaction.id.desc())
                    .first()
                )
            original_paid = float(refund_tx.amount) if refund_tx else 0.0

            deducted = []
            for item in order.items:
                product_name = item.product.name if item.product else str(item.product_id)
                stock = db.query(WarehouseStock).filter(
                    WarehouseStock.warehouse_id == item.warehouse_id,
                    WarehouseStock.product_id == item.product_id,
                ).first()
                available = int(stock.quantity) if stock else 0
                need = int(item.quantity or 0)
                if need <= 0:
                    continue
                if not stock or available < need:
                    db.rollback()
                    return False, f"\u5e93\u5b58\u4e0d\u8db3\uff0c\u65e0\u6cd5\u6062\u590d {product_name}\uff1a\u9700\u8981 {need}\uff0c\u5269\u4f59 {available}"

            user = db.query(User).filter(User.id == order.user_id).first() if order.user_id else None
            if refund_tx and user and float(user.balance or 0) + 1e-9 < original_paid:
                return False, f"\u7528\u6237\u9884\u5b58\u4e0d\u8db3\uff0c\u65e0\u6cd5\u6263\u56de\u9000\u6b3e {original_paid}"

            for item in order.items:
                need = int(item.quantity or 0)
                if need <= 0:
                    continue
                stock = db.query(WarehouseStock).filter(
                    WarehouseStock.warehouse_id == item.warehouse_id,
                    WarehouseStock.product_id == item.product_id,
                ).first()
                avg_cost, cost_amount, _details = self.consume_batches_fifo(
                    db, item.product_id, item.warehouse_id, need
                )
                stock.quantity = int(stock.quantity or 0) - need
                import json
                unknown=any(d['unit_cost'] is None for d in _details)
                item.cost_status='COST_INCOMPLETE' if unknown else 'COST_COMPLETE'
                item.unknown_cost_quantity=sum(d['qty'] for d in _details if d['unit_cost'] is None)
                item.fifo_details=json.dumps(_details)
                item.unit_cost = avg_cost or 0.0
                item.cost_amount = cost_amount or 0.0
                item.profit=0.0 if unknown else item.quantity*item.unit_price-item.cost_amount
                db.add(StockMovement(
                    product_id=item.product_id,
                    warehouse_id=item.warehouse_id,
                    change_qty=-need,
                    movement_type="order_out",
                    ref_type="order",
                    ref_id=order.order_number,
                    note="\u6062\u590d\u8ba2\u5355\u91cd\u65b0\u51fa\u5e93",
                ))
                self.refresh_product_stock_status(db, item.product_id)
                deducted.append({
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else "",
                    "warehouse_id": item.warehouse_id,
                    "quantity": need,
                })

            if refund_tx and user and original_paid > 0:
                user.balance = float(user.balance or 0) - original_paid
                desc = f"\u6062\u590d\u8ba2\u5355\u6263\u56de\u9000\u6b3e #{order.order_number}"
                if reason:
                    desc += f"\uff0c\u539f\u56e0: {reason}"
                db.add(Transaction(
                    user_id=user.id,
                    amount=-original_paid,
                    type="restore_charge",
                    description=desc,
                ))

            from address_daily_orders import restored_status
            order.status = restored_status(db,order,datetime.utcnow(),to_status)
            order.cost_status='COST_INCOMPLETE' if any(i.cost_status!='COST_COMPLETE' for i in order.items) else 'COST_COMPLETE'
            order.total_profit=0.0 if order.cost_status!='COST_COMPLETE' else sum(i.profit for i in order.items)
            order.paid_amount = original_paid
            grand = float(order.grand_total or 0)
            if original_paid <= 0:
                order.payment_status = "unpaid"
            elif original_paid + 1e-9 >= grand:
                order.payment_status = "paid"
            else:
                order.payment_status = "partial"
            extra = "[\u6062\u590d] " + (reason or "\u540e\u53f0\u6062\u590d\u5df2\u53d6\u6d88\u8ba2\u5355")
            note = order.notes or ""
            order.notes = (note + chr(10) + extra).strip() if note else extra

            db.commit()
            db.refresh(order)
            return True, {
                "order_id": order.id,
                "order_number": order.order_number,
                "status": order.status,
                "paid_amount": order.paid_amount,
                "payment_status": order.payment_status,
                "deducted_stock": deducted,
                "reversed_refund": original_paid,
            }
        except Exception as e:
            db.rollback()
            return False, str(e)


        finally:
            # Early validation returns also hold BEGIN IMMEDIATE until closed.
            # Never leave a writer locked for the next item in a batch.
            db.close()

    def get_user_unit_price(self, user_id: int, product: Product) -> float:
        """口味专属价 > 分类专属价 > 当前等级价；连接必须归还池。"""
        with self.SessionLocal() as db:
            custom = db.query(UserProductPrice).filter_by(user_id=user_id,product_id=product.id).first()
            if custom is not None:return __import__('dynamic_prices').custom(product,custom.unit_price)
            category=db.query(UserCategoryPrice).filter_by(user_id=user_id,parent_id=product.parent_id).first() if product.parent_id else None
            if category is not None:return __import__('dynamic_prices').custom(product,category.unit_price)
            user = db.query(User).filter(User.id == user_id).first()
            return product.get_price_for_agent_level(user.agent_level if user else 0)

    def set_user_product_price(self, user_id: int, product_id: int, unit_price: float):
        db = next(self.get_db())
        row = db.query(UserProductPrice).filter(
            UserProductPrice.user_id == user_id,
            UserProductPrice.product_id == product_id
        ).first()
        if row:
            row.unit_price = unit_price
        else:
            row = UserProductPrice(user_id=user_id, product_id=product_id, unit_price=unit_price)
            db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def get_user_product_prices(self, user_id: int):
        db = next(self.get_db())
        rows = db.query(UserProductPrice, Product).join(
            Product, UserProductPrice.product_id == Product.id
        ).filter(UserProductPrice.user_id == user_id).all()
        return [{
            'id': r.UserProductPrice.id,
            'user_id': user_id,
            'product_id': r.Product.id,
            'product_name': r.Product.name,
            'unit_price': r.UserProductPrice.unit_price,
            'updated_at': r.UserProductPrice.updated_at,
        } for r in rows]

    def calc_user_debt(self, user_id: int, session: Session = None) -> float:
        """计算用户欠款 = 未取消订单的 (grand_total - paid_amount) 之和"""
        own_session = session is None
        db = session or self.SessionLocal()
        try:
            outstanding = case(
                (Order.grand_total > Order.paid_amount, Order.grand_total - Order.paid_amount),
                else_=0.0,
            )
            debt = db.query(
                func.coalesce(func.sum(outstanding), 0.0)
            ).filter(
                Order.user_id == user_id,
                Order.status != 'cancelled'
            ).scalar()
            from manual_receivables import outstanding as manual_outstanding
            return round(float(debt or 0.0) + manual_outstanding(db,user_id), 2)
        finally:
            if own_session:
                db.close()

    def _refresh_order_payment_status(self, order: Order):
        paid = order.paid_amount or 0
        total = order.grand_total or 0
        if paid <= 0:
            order.payment_status = 'unpaid'
        elif paid + 1e-9 >= total:
            order.payment_status = 'paid'
            order.paid_amount = total
        else:
            order.payment_status = 'partial'

    def record_payment(self, user_id: int, amount: float, method: str = 'cny',
                       order_id: int = None, note: str = None, operator: str = None,
                       original_amount: float = None, exchange_rate: float = None,
                       exchange_currency: str = None, operation_key: str = None):
        """人工收账。

        - 若指定 order_id：优先冲该订单
        - 否则按时间从早到晚冲未结清订单
        - 多余部分进入用户预存 balance
        """
        if not math.isfinite(float(amount)) or amount <= 0:
            return False, '收款金额必须大于0'

        db = next(self.get_db())
        try:
            if operation_key:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                from telegram_accounts import begin_operation
                prior=begin_operation(db,operation_key,user_id,'payment',dict(
                    amount=original_amount if original_amount is not None else amount,
                    method=method,order_id=order_id,note=note))
                if prior is not None:return True,prior
            else:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False, '用户不存在'

            method = (method or 'cny').lower()
            if method not in ('cny', 'usdt', 'other'):
                method = 'other'

            remain = float(amount)
            affected_orders = []

            targets = []
            if order_id:
                order = db.query(Order).filter(Order.id == order_id, Order.user_id == user_id).first()
                if not order:
                    return False, '订单不存在或不属于该用户'
                if order.status == 'cancelled':
                    return False, '已取消订单不能收款'
                targets = [order]
            else:
                targets = db.query(Order).filter(
                    Order.user_id == user_id,
                    Order.status != 'cancelled'
                ).order_by(Order.created_at.asc()).all()
                targets = [o for o in targets if (o.grand_total or 0) - (o.paid_amount or 0) > 1e-9]

            for order in targets:
                due = (order.grand_total or 0) - (order.paid_amount or 0)
                if due <= 1e-9 or remain <= 1e-9:
                    continue
                pay = min(due, remain)
                order.paid_amount = (order.paid_amount or 0) + pay
                self._refresh_order_payment_status(order)
                remain -= pay
                affected_orders.append({
                    'order_id': order.id,
                    'order_number': order.order_number,
                    'paid': pay,
                    'payment_status': order.payment_status,
                    'remain': max(0, (order.grand_total or 0) - (order.paid_amount or 0))
                })

            receivable_allocations=[]
            if not order_id:
                from manual_receivables import allocate
                remain,receivable_allocations=allocate(db,user_id,remain)

            # 多收部分进预存
            if remain > 1e-9:
                remain = round(remain, 2)
                user.balance += remain
                user.balance = round(float(user.balance or 0), 2)
                db.add(Transaction(
                    user_id=user.id,
                    amount=remain,
                    type='recharge',
                    description=f'人工收款超额转入预存（{method}）'
                ))

            payment = Payment(
                user_id=user_id,
                order_id=order_id,
                amount=amount,
                method=method,
                original_amount=original_amount,
                exchange_rate=exchange_rate,
                exchange_currency=exchange_currency,
                note=note,
                operator=operator,
            )
            db.add(payment)
            db.flush()
            from manual_receivables import record_allocations
            record_allocations(db,payment.id,receivable_allocations)
            result = {
                'payment_id': payment.id,
                'user_id': user_id,
                'amount': amount,
                'method': method,
                'original_amount': original_amount,
                'exchange_rate': exchange_rate,
                'exchange_currency': exchange_currency,
                'affected_orders': affected_orders,
                'affected_receivables': receivable_allocations,
                'prepaid_added': round(remain, 2) if remain > 1e-9 else 0,
                'user_balance': user.balance,
                'user_debt': self.calc_user_debt(user_id, session=db),
            }
            if operation_key:
                from telegram_accounts import finish_operation,queue_notice
                from account_notifications import payment_text
                from telegram_accounts import summary
                result['bill_snapshot']=summary(db,user_id)
                from cumulative_bill_snapshot import capture
                result['cumulative_bill_snapshot']=capture(db,user_id,note=f'本次收款 ¥{amount:.2f}')
                finish_operation(db,operation_key,result)
                queue_notice(db,'payment:'+str(payment.id),user_id,'payment',{'text':payment_text(result)})
            db.commit()
            return True,result
        except Exception as e:
            db.rollback()
            return False, str(e)
        finally:
            db.close()

    def apply_prepaid_balance(self, user_id: int, amount: float = None,
                              order_id: int = None, note: str = None, operator: str = None,
                              operation_key: str = None):
        """使用用户预存余额冲减欠款。

        - 不产生新的现金收款，只减少 User.balance。
        - 若指定 order_id：优先冲该订单。
        - 否则按时间从早到晚冲未结清订单。
        """
        db = next(self.get_db())
        try:
            if operation_key:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                from telegram_accounts import begin_operation
                prior = begin_operation(db, operation_key, user_id, 'prepaid_apply',
                                        dict(amount=amount, order_id=order_id, note=note))
                if prior is not None:
                    return True, prior
            else:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False, '用户不存在'
            balance = float(user.balance or 0)
            if balance <= 1e-9:
                return False, '该用户没有可抵扣预存余额'

            requested = float(amount) if amount is not None else balance
            if not math.isfinite(requested) or requested <= 0:
                return False, '抵扣金额必须大于0'
            remain = min(balance, requested)
            original_remain = remain
            affected_orders = []

            if order_id:
                order = db.query(Order).filter(Order.id == order_id, Order.user_id == user_id).first()
                if not order:
                    return False, '订单不存在或不属于该用户'
                if order.status == 'cancelled':
                    return False, '已取消订单不能抵扣'
                targets = [order]
            else:
                targets = db.query(Order).filter(
                    Order.user_id == user_id,
                    Order.status != 'cancelled'
                ).order_by(Order.created_at.asc()).all()
                targets = [o for o in targets if (o.grand_total or 0) - (o.paid_amount or 0) > 1e-9]

            from manual_receivables import outstanding as manual_outstanding
            if not targets and (order_id or manual_outstanding(db,user_id)<=0):
                return False, '该用户没有可抵扣欠款订单'

            for order in targets:
                due = (order.grand_total or 0) - (order.paid_amount or 0)
                if due <= 1e-9 or remain <= 1e-9:
                    continue
                pay = min(due, remain)
                order.paid_amount = (order.paid_amount or 0) + pay
                self._refresh_order_payment_status(order)
                remain -= pay
                affected_orders.append({
                    'order_id': order.id,
                    'order_number': order.order_number,
                    'paid': round(pay, 2),
                    'payment_status': order.payment_status,
                    'remain': round(max(0, (order.grand_total or 0) - (order.paid_amount or 0)), 2),
                })

            receivable_allocations=[]
            if not order_id:
                from manual_receivables import allocate
                remain,receivable_allocations=allocate(db,user_id,remain)
            used = round(original_remain - remain, 2)
            if used <= 1e-9:
                return False, '预存余额未抵扣到任何欠款'

            user.balance = balance - used
            db.add(Transaction(
                user_id=user.id,
                amount=-used,
                type='prepaid_deduct',
                description=note or '使用预存余额冲减欠款'
            ))
            payment = Payment(
                user_id=user_id,
                order_id=order_id,
                amount=used,
                method='prepaid',
                note=note or '预存余额抵扣欠款',
                operator=operator,
            )
            db.add(payment)
            db.flush()
            result = {
                'payment_id': payment.id,
                'user_id': user_id,
                'amount': used,
                'method': 'prepaid',
                'affected_orders': affected_orders,
                'affected_receivables': receivable_allocations,
                'user_balance': round(user.balance or 0, 2),
                'user_debt': self.calc_user_debt(user_id, session=db),
            }
            from manual_receivables import record_allocations
            record_allocations(db,payment.id,receivable_allocations)
            if operation_key:
                from telegram_accounts import finish_operation, queue_notice
                from cumulative_bill_snapshot import capture
                result['cumulative_bill_snapshot']=capture(db,user_id,note=f'预存抵扣 ¥{used:.2f}')
                message = result['cumulative_bill_snapshot']['text']
                finish_operation(db, operation_key, result)
                queue_notice(db, 'payment:'+str(payment.id), user_id, 'prepaid', {'text':message})
            db.commit()
            return True, result
        except Exception as e:
            db.rollback()
            return False, str(e)
        finally:
            db.close()

    def adjust_user_balance(self, user_id: int, mode: str, amount: float,
                            note: str = None, operator: str = None, operation_key: str = None):
        """人工调整用户预存余额。

        mode:
        - add: 增加预存
        - reduce: 减少预存
        - set: 直接设为指定余额
        """
        mode = (mode or '').strip().lower()
        if mode not in ('add', 'reduce', 'set'):
            return False, '调整方式只支持 add / reduce / set'
        try:
            amount = round(float(amount), 2)
        except Exception:
            return False, '金额格式错误'
        if not math.isfinite(amount) or amount < 0:
            return False, '金额不能小于0'
        if not (note or '').strip():
            return False, '请填写调整原因/备注'

        db = next(self.get_db())
        try:
            if operation_key:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                from telegram_accounts import begin_operation
                prior=begin_operation(db,operation_key,user_id,'balance_adjust',dict(mode=mode,amount=amount,note=note))
                if prior is not None:return True,prior
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False, '用户不存在'
            old_balance = round(float(user.balance or 0), 2)
            if mode == 'add':
                delta = amount
                new_balance = old_balance + delta
                action = '增加预存'
            elif mode == 'reduce':
                if amount > old_balance + 1e-9:
                    return False, f'减少金额不能超过当前预存余额 ¥{old_balance:.2f}'
                delta = -amount
                new_balance = old_balance + delta
                action = '减少预存'
            else:
                new_balance = amount
                delta = round(new_balance - old_balance, 2)
                action = '设置预存'

            user.balance = round(new_balance, 2)
            transaction=Transaction(
                user_id=user.id,
                amount=delta,
                type='balance_adjust',
                description=f'{action}: {old_balance:.2f} -> {user.balance:.2f}; 原因: {note}; 操作人: {operator or "admin"}'
            )
            db.add(transaction);db.flush()
            result = {
                'transaction_id':transaction.id,
                'user_id': user.id,
                'mode': mode,
                'amount': amount,
                'delta': round(delta, 2),
                'old_balance': old_balance,
                'new_balance': round(user.balance or 0, 2),
                'user_debt': self.calc_user_debt(user.id, session=db),
            }
            if operation_key:
                from telegram_accounts import finish_operation,queue_notice
                from cumulative_bill_snapshot import capture
                result['cumulative_bill_snapshot']=capture(db,user_id,note=f'{action}；变动 {delta:+.2f}元；缘由：{note}')
                finish_operation(db,operation_key,result)
                queue_notice(db,'balance:'+str(transaction.id),user_id,'balance',{'text':result['cumulative_bill_snapshot']['text']})
            db.commit()
            return True,result
        except Exception as e:
            db.rollback()
            return False, str(e)

        finally:
            db.close()

    def add_stock_movement(self, product_id: int, warehouse_id: int, change_qty: int,
                           movement_type: str, ref_type: str = None, ref_id: str = None,
                           note: str = None, unit_cost: float = None, batch_no: str = None,
                           remain_qty: int = None, session=None):
        own = session is None
        db = session or next(self.get_db())
        mv = StockMovement(
            product_id=product_id,
            warehouse_id=warehouse_id,
            change_qty=change_qty,
            movement_type=movement_type,
            unit_cost=unit_cost,
            batch_no=batch_no,
            remain_qty=remain_qty,
            ref_type=ref_type,
            ref_id=ref_id,
            note=note,
        )
        db.add(mv)
        if own:
            db.commit()
            db.refresh(mv)
        else:
            db.flush()
        return mv

    def _gen_batch_no(self) -> str:
        return datetime.utcnow().strftime("B%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()

    def inbound_stock(self, product_id: int, warehouse_id: int, quantity: int,
                      unit_cost: float = None, note: str = None, batch_no: str = None,
                      update_product_cost: bool = True, session: Session = None) -> dict:
        """到货入库：增加库存 + 记录批次成本 + 流水。"""
        if quantity <= 0:
            return {"success": False, "error": "入库数量必须大于0"}
        own_session = session is None
        db = session or next(self.get_db())
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            warehouse = db.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
            if not product:
                return {"success": False, "error": "商品不存在"}
            if product.is_parent:
                return {"success": False, "error": "分类不能入库"}
            if not warehouse:
                return {"success": False, "error": "仓库不存在"}
            if product.default_warehouse_id != warehouse_id:
                return {"success": False, "error": "商品只能在绑定仓库入库"}

            import math
            if unit_cost is None:
                from inventory_reference import reference_amount
                if reference_amount(1,product.cost_price,product.reference_price_state)['reference_incomplete']:
                    return {"success": False, "error": "请填写本批进货价，或先设置商品成本"}
                unit_cost=product.cost_price
            if isinstance(unit_cost, bool):
                return {"success": False, "error": "本批进货价必须是有限非负数"}
            cost = float(unit_cost)
            if not math.isfinite(cost) or cost < 0:
                return {"success": False, "error": "实际单位成本必须是有限非负数"}
            from inventory_costs import capture
            capture(db, product_id, warehouse_id)
            bno = batch_no or self._gen_batch_no()

            stock = db.query(WarehouseStock).filter(
                WarehouseStock.warehouse_id == warehouse_id,
                WarehouseStock.product_id == product_id
            ).first()
            before = stock.quantity if stock else 0
            if not stock:
                stock = WarehouseStock(warehouse_id=warehouse_id, product_id=product_id, quantity=quantity)
                db.add(stock)
            else:
                stock.quantity = int(stock.quantity or 0) + int(quantity)
            after = stock.quantity

            batch = InventoryBatch(
                product_id=product_id,
                warehouse_id=warehouse_id,
                batch_no=bno,
                unit_cost=cost,
                quantity=int(quantity),
                remain_qty=int(quantity),
                cost_source='VERIFIED_REAL_COST',
                note=note,
            )
            db.add(batch)
            db.flush()

            # 商品参考价与实际批次成本独立；入库不覆盖参考价或历史成本。

            mv = StockMovement(
                product_id=product_id,
                warehouse_id=warehouse_id,
                change_qty=int(quantity),
                movement_type="inbound",
                unit_cost=cost,
                batch_no=bno,
                remain_qty=int(quantity),
                ref_type="batch",
                ref_id=str(batch.id),
                note=note or f"入库 {quantity} 成本{cost}",
            )
            db.add(mv)
            self.refresh_product_stock_status(db, product_id)
            if own_session:
                db.commit()
            return {
                "success": True,
                "batch_id": batch.id,
                "batch_no": bno,
                "product_id": product_id,
                "warehouse_id": warehouse_id,
                "quantity": int(quantity),
                "unit_cost": cost,
                "before": before,
                "after": after,
                "product_name": product.name,
                "warehouse_name": warehouse.name,
            }
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}

        finally:
            if own_session:
                db.close()

    def consume_batches_fifo(self, db, product_id: int, warehouse_id: int, quantity: int):
        """按下单出库：按先进先出扣批次，返回 (avg_unit_cost, cost_amount, details)."""
        need = int(quantity)
        if need <= 0:
            return 0.0, 0.0, []
        batches = db.query(InventoryBatch).filter(
            InventoryBatch.product_id == product_id,
            InventoryBatch.warehouse_id == warehouse_id,
            InventoryBatch.remain_qty > 0,
            InventoryBatch.cost_source.in_(('VERIFIED_REAL_COST','RESTORED_VERIFIED_FIFO')),
        ).order_by(InventoryBatch.fifo_priority.asc(), InventoryBatch.created_at.asc(), InventoryBatch.id.asc()).all()
        left = need
        total_cost = 0.0
        from inventory_costs import consume_unknown, ready
        left, details = consume_unknown(db, product_id, warehouse_id, need)
        if not ready(db):
            stock = db.query(WarehouseStock).filter_by(product_id=product_id,warehouse_id=warehouse_id).first()
            missing = max(0, int(stock.quantity or 0) - sum(int(b.remain_qty or 0) for b in batches)) if stock else 0
            take = min(left,missing)
            if take:
                details.append({'batch_id':None,'batch_no':'HISTORICAL_UNKNOWN','qty':take,'unit_cost':None})
                left -= take
        for b in batches:
            if left <= 0:
                break
            take = min(int(b.remain_qty or 0), left)
            if take <= 0:
                continue
            b.remain_qty = int(b.remain_qty or 0) - take
            total_cost += take * float(b.unit_cost or 0)
            details.append({
                "batch_id": b.id,
                "batch_no": b.batch_no,
                "qty": take,
                "unit_cost": float(b.unit_cost or 0),
                "cost_source": b.cost_source,
            })
            left -= take
        # No reference-price fallback. Missing cost remains explicitly unknown.
        if left > 0:
            if ready(db):
                raise ValueError('库存数量与成本层不守恒，请先核查历史成本；本次操作未执行')
            details.append({
                "batch_id": None,
                "batch_no": "HISTORICAL_UNKNOWN",
                "qty": left,
                "unit_cost": None,
            })
            left = 0
        unknown = any(d['unit_cost'] is None for d in details)
        return (None if unknown else total_cost / need), (None if unknown else total_cost), details

    def restore_batches_on_cancel(self, db, product_id: int, warehouse_id: int, quantity: int, unit_cost: float = 0.0):
        """取消订单时，把数量以原成本回补为一个新批次（简化可追溯）。"""
        qty = int(quantity)
        if qty <= 0:
            return
        bno = self._gen_batch_no()
        batch = InventoryBatch(
            product_id=product_id,
            warehouse_id=warehouse_id,
            batch_no=bno,
            unit_cost=float(unit_cost or 0),
            quantity=qty,
            remain_qty=qty,
            note="订单取消回滚",
        )
        db.add(batch)
        db.flush()
        db.add(StockMovement(
            product_id=product_id,
            warehouse_id=warehouse_id,
            change_qty=qty,
            movement_type="cancel_in",
            unit_cost=float(unit_cost or 0),
            batch_no=bno,
            remain_qty=qty,
            ref_type="batch",
            ref_id=str(batch.id),
            note="取消回滚入库",
        ))

    def list_inbound_records(self, day: str = None, product_id: int = None, warehouse_id: int = None, limit: int = 200):
        from datetime import datetime as dt, timedelta
        db = next(self.get_db())
        q = db.query(StockMovement).filter(StockMovement.change_qty > 0, StockMovement.movement_type.in_(["inbound", "cancel_in", "adjust"]))
        if day:
            start = dt.strptime(day, "%Y-%m-%d")
            end = start + timedelta(days=1)
            q = q.filter(StockMovement.created_at >= start, StockMovement.created_at < end)
        if product_id:
            q = q.filter(StockMovement.product_id == product_id)
        if warehouse_id:
            q = q.filter(StockMovement.warehouse_id == warehouse_id)
        rows = q.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).limit(limit).all()
        result = []
        for m in rows:
            result.append({
                "id": m.id,
                "product_id": m.product_id,
                "product_name": m.product.name if m.product else str(m.product_id),
                "parent_id": m.product.parent_id if m.product and m.product.parent else None,
                "parent_name": m.product.parent.name if m.product and m.product.parent else "",
                "warehouse_id": m.warehouse_id,
                "warehouse_name": m.warehouse.name if m.warehouse else str(m.warehouse_id),
                "quantity": m.change_qty,
                "unit_cost": m.unit_cost,
                "batch_no": m.batch_no,
                "movement_type": m.movement_type,
                "note": m.note,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "amount": (m.change_qty or 0) * float(m.unit_cost or 0),
            })
        return result

    def inventory_value_summary(self):
        """Known FIFO value only; missing historical cost is reported, never estimated."""
        db = next(self.get_db())
        batches = db.query(InventoryBatch).filter(InventoryBatch.remain_qty > 0,
            InventoryBatch.cost_source.in_(('VERIFIED_REAL_COST','RESTORED_VERIFIED_FIFO'))).all()
        by_wh = {}
        by_product = {}
        total = 0.0
        for b in batches:
            val = float(b.remain_qty or 0) * float(b.unit_cost or 0)
            total += val
            wname = b.warehouse.name if b.warehouse else str(b.warehouse_id)
            pname = b.product.name if b.product else str(b.product_id)
            by_wh[wname] = by_wh.get(wname, 0.0) + val
            key = f"{pname}@{wname}"
            by_product[key] = by_product.get(key, 0.0) + val
        # fallback stocks without batch remain
        stocks = db.query(WarehouseStock).filter(WarehouseStock.quantity > 0).all()
        covered = {}
        for b in batches:
            covered[(b.product_id, b.warehouse_id)] = covered.get((b.product_id, b.warehouse_id), 0) + int(b.remain_qty or 0)
        for s in stocks:
            used = covered.get((s.product_id, s.warehouse_id), 0)
            extra = int(s.quantity or 0) - int(used)
            if extra > 0:
                # Unknown stock contributes no fabricated value. Report separately.
                pass
        from inventory_costs import listing
        missing = listing(db)
        return {
            "total_value": None if missing else round(total, 2),
            "known_value": round(total,2),
            "cost_status": 'COST_INCOMPLETE' if missing else 'COST_COMPLETE',
            "unknown_quantity": sum(r['unknown_on_hand'] for r in missing),
            "unresolved_consumed_quantity": sum(r['unknown_consumed'] for r in missing),
            "incomplete_inventory": missing,
            "by_warehouse": {k: round(v, 2) for k, v in by_wh.items()},
            "by_product": {k: round(v, 2) for k, v in sorted(by_product.items(), key=lambda x: -x[1])[:100]},
        }


    def current_inventory_reference(self):
        """Current catalog-price estimate only; never changes historical FIFO facts."""
        from inventory_reference import reference_summary
        with self.SessionLocal() as session:
            rows=[];warehouses={};categories={}
            for stock in session.query(WarehouseStock).filter(WarehouseStock.quantity >= 0).all():
                product=stock.product
                row=dict(quantity=stock.quantity,cost_price=product.cost_price if product else None,
                         reference_price_state=product.reference_price_state if product else 'UNSET')
                rows.append(row)
                key=stock.warehouse_id
                bucket=warehouses.setdefault(key,dict(id=key,name=stock.warehouse.name if stock.warehouse else str(key),rows=[]))
                bucket['rows'].append(row)
                category=product.parent if product else None
                ck=(key,category.id if category else None)
                cb=categories.setdefault(ck,dict(warehouse_id=key,category_id=ck[1],name=category.name if category else '未分类',rows=[]))
                cb['rows'].append(row)
            result=reference_summary(rows)
            result['warehouses']=[dict(id=g['id'],name=g['name'],**reference_summary(g['rows'])) for g in warehouses.values()]
            result['categories']=[dict(warehouse_id=g['warehouse_id'],category_id=g['category_id'],name=g['name'],**reference_summary(g['rows'])) for g in categories.values()]
            return result


    def period_stats(self, period: str = "day", ref: str = None):
        """period=day|month|quarter；返回盈利、入库、发货、货值。"""
        from datetime import datetime as dt, timedelta
        from calendar import monthrange
        db = next(self.get_db())
        from business_time_range import business_now,OFFSET
        now = business_now()
        period = (period or "day").lower()

        if period == "month":
            if ref:
                start = dt.strptime(ref[:7] + "-01", "%Y-%m-%d")
            else:
                start = dt(now.year, now.month, 1)
            last = monthrange(start.year, start.month)[1]
            end = dt(start.year, start.month, last, 23, 59, 59)
            label = start.strftime("%Y-%m")
        elif period == "quarter":
            if ref:
                ref_u = ref.upper().replace(" ", "")
                if "Q" in ref_u:
                    year = int(re.findall(r"\d{4}", ref_u)[0])
                    q = int(re.findall(r"Q(\d)", ref_u)[0])
                else:
                    parts = re.findall(r"\d+", ref_u)
                    year = int(parts[0])
                    q = int(parts[1]) if len(parts) > 1 else ((now.month - 1) // 3 + 1)
                    if q > 4:
                        q = (q - 1) // 3 + 1
            else:
                year = now.year
                q = (now.month - 1) // 3 + 1
            start_month = (q - 1) * 3 + 1
            start = dt(year, start_month, 1)
            end_month = start_month + 2
            last = monthrange(year, end_month)[1]
            end = dt(year, end_month, last, 23, 59, 59)
            label = f"{year}-Q{q}"
        else:
            if ref:
                start = dt.strptime(ref[:10], "%Y-%m-%d")
            else:
                start = dt(now.year, now.month, now.day)
            end = start + timedelta(days=1) - timedelta(seconds=1)
            label = start.strftime("%Y-%m-%d")

        local_start,local_end=start,end
        start,end=start-OFFSET,end+timedelta(seconds=1)-OFFSET
        orders = db.query(Order).filter(
            Order.created_at >= start,
            Order.created_at < end,
            Order.status != "cancelled",
        ).all()
        shipped = db.query(Order).filter(
            Order.shipped_at >= start,
            Order.shipped_at < end,
        ).all()
        movements = db.query(StockMovement).filter(
            StockMovement.created_at >= start,
            StockMovement.created_at < end,
        ).all()
        from sqlalchemy import inspect, text
        cancelled_inbound=set()
        if inspect(db.connection()).has_table('stock_operations'):
            cancelled_inbound={r[0] for r in db.execute(text(
                "SELECT id FROM stock_operations WHERE operation_type='inbound' AND status='cancelled'"))}
        inbound = [m for m in movements
            if (m.change_qty or 0)>0 and m.movement_type in ("inbound","adjust")
            and m.batch_no not in cancelled_inbound]
        payments = db.query(Payment).filter(
            Payment.created_at >= start,
            Payment.created_at < end,
        ).all()

        sales_amount = sum(float(o.total_amount or 0) for o in orders)
        shipping_amount = sum(float(o.total_shipping_fee or 0) for o in orders)
        from inventory_costs import order_summary
        costs=[order_summary(db,o) for o in orders]
        incomplete_count=sum(c['cost_status']!='COST_COMPLETE' for c in costs)
        known_profit=sum(c['total_profit'] for c in costs if c['total_profit'] is not None)
        known_cost=sum(c['known_cost_amount'] for c in costs)

        inbound_qty = sum(int(m.change_qty or 0) for m in inbound)
        trusted_inbound_keys={(b.batch_no,b.product_id,b.warehouse_id) for b in db.query(InventoryBatch).all()
                              if b.cost_source in ('VERIFIED_REAL_COST','RESTORED_VERIFIED_FIFO')}
        def verified_inbound(m):
            return m.unit_cost is not None and (m.batch_no,m.product_id,m.warehouse_id) in trusted_inbound_keys
        inbound_cost = sum(int(m.change_qty or 0) * float(m.unit_cost or 0) for m in inbound if verified_inbound(m))
        inbound_unknown_qty=sum(int(m.change_qty or 0) for m in inbound if not verified_inbound(m))
        stock_value = self.inventory_value_summary()
        stock_reference = self.current_inventory_reference()

        return {
            "period": period,
            "label": label,
            "start": local_start.isoformat(),
            "end": local_end.isoformat(),
            "timezone": "Asia/Shanghai",
            "orders_count": len(orders),
            "orders_amount": round(sales_amount, 2),
            "shipping_amount": round(shipping_amount, 2),
            "grand_amount": round(sales_amount + shipping_amount, 2),
            "profit_amount": None if incomplete_count else round(known_profit,2),
            "cost_amount": None if incomplete_count else round(known_cost,2),
            "known_profit_amount": round(known_profit,2),
            "known_cost_amount": round(known_cost,2),
            "cost_status": 'COST_INCOMPLETE' if incomplete_count else 'COST_COMPLETE',
            "cost_label": '历史进货价缺失' if incomplete_count else '销售成本已记录',
            "cost_incomplete_orders": incomplete_count,
            "shipped_count": len(shipped),
            "shipped_amount": round(sum(float(o.grand_total or 0) for o in shipped), 2),
            "inbound_count": len(inbound),
            "inbound_qty": inbound_qty,
            "inbound_cost": None if inbound_unknown_qty else round(inbound_cost,2),
            "inbound_known_cost": round(inbound_cost,2),
            "inbound_unknown_cost_quantity": inbound_unknown_qty,
            "payments_amount": round(sum(float(p.amount or 0) for p in payments if p.method != 'prepaid'), 2),
            "prepaid_deduct_amount": round(sum(float(p.amount or 0) for p in payments if p.method == 'prepaid'), 2),
            "stock_value_total": stock_value["total_value"],
            "stock_known_value": stock_value['known_value'],
            "stock_cost_status": stock_value['cost_status'],
            "stock_unknown_quantity": stock_value['unknown_quantity'],
            "stock_value_by_warehouse": stock_value["by_warehouse"],
            "stock_reference": stock_reference,
        }

    def sales_breakdown(self, period: str = "day", ref: str = None, shipped_only: bool = False):
        """按日/月/季度汇总已下单商品的品类、口味和销售数量。

        统计按订单创建时间归属区间，取消订单不计入；订单明细保留商品父分类，
        这样同名口味在不同品类下会分别统计。返回结果已按数量、销售额降序排列。
        """
        from datetime import datetime as dt, timedelta
        from calendar import monthrange

        db = next(self.get_db())
        from business_time_range import business_now,OFFSET
        now = business_now()
        period = (period or "day").lower()
        if period == "month":
            start = dt.strptime(ref[:7] + "-01", "%Y-%m-%d") if ref else dt(now.year, now.month, 1)
            end = dt(start.year, start.month, monthrange(start.year, start.month)[1]) + timedelta(days=1)
            label = start.strftime("%Y-%m")
        elif period == "quarter":
            if ref:
                ref_u = ref.upper().replace(" ", "")
                if "Q" in ref_u:
                    year = int(re.findall(r"\d{4}", ref_u)[0])
                    quarter = int(re.findall(r"Q(\d)", ref_u)[0])
                else:
                    parts = re.findall(r"\d+", ref_u)
                    year = int(parts[0])
                    quarter = int(parts[1]) if len(parts) > 1 else ((now.month - 1) // 3 + 1)
            else:
                year = now.year
                quarter = (now.month - 1) // 3 + 1
            quarter = min(4, max(1, quarter))
            start_month = (quarter - 1) * 3 + 1
            start = dt(year, start_month, 1)
            end_month = start_month + 2
            end = dt(year, end_month, monthrange(year, end_month)[1]) + timedelta(days=1)
            label = f"{year}-Q{quarter}"
        else:
            period = "day"
            start = dt.strptime(ref[:10], "%Y-%m-%d") if ref else dt(now.year, now.month, now.day)
            end = start + timedelta(days=1)
            label = start.strftime("%Y-%m-%d")

        local_start,local_end=start,end
        start,end=start-OFFSET,end-OFFSET
        orders = db.query(Order).filter(
            Order.created_at >= start,
            Order.created_at < end,
            Order.status != "cancelled",
        )
        if shipped_only:
            orders = orders.filter(Order.status == "shipped")
        orders = orders.all()
        buckets = {}
        for order in orders:
            for item in (order.items or []):
                quantity = int(item.quantity or 0)
                if quantity <= 0:
                    continue
                product = item.product
                parent = product.parent if product and product.parent else None
                parent_id = parent.id if parent else None
                parent_name = (parent.name if parent and parent.name else "默认分类")
                parent_sort = (
                    int(parent.category_serial)
                    if parent and parent.category_serial is not None
                    else int(parent.id)
                    if parent and parent.id is not None
                    else 0
                )
                product_id = product.id if product else item.product_id
                product_name = (product.name if product and product.name else str(item.product_id))
                key = (parent_id, parent_name, product_id, product_name)
                row = buckets.setdefault(key, {
                    "parent_id": parent_id,
                    "parent_name": parent_name,
                    "_parent_sort": parent_sort,
                    "product_id": product_id,
                    "product_name": product_name,
                    "quantity": 0,
                    "orders_count": 0,
                    "sales_amount": 0.0,
                    "_order_ids": set(),
                })
                row["quantity"] += quantity
                row["sales_amount"] += quantity * float(item.unit_price or 0)
                row["_order_ids"].add(order.id)

        items = []
        for row in buckets.values():
            row["orders_count"] = len(row.pop("_order_ids"))
            row["sales_amount"] = round(row["sales_amount"], 2)
            items.append(row)
        # Keep every category contiguous; category_serial follows the Cxxx catalog order.
        items.sort(key=lambda row: (
            row["_parent_sort"],
            row["parent_name"],
            -row["quantity"],
            -row["sales_amount"],
            row["product_name"],
        ))
        for row in items:
            row.pop("_parent_sort", None)
        return {
            "period": period,
            "label": label,
            "start": local_start.isoformat(),
            "end": (local_end - timedelta(seconds=1)).isoformat(),
            "timezone": "Asia/Shanghai",
            "total_quantity": sum(row["quantity"] for row in items),
            "total_orders": len(orders),
            "total_amount": round(sum(row["sales_amount"] for row in items), 2),
            "items": items,
        }

    def overdue_unpaid_orders(self, days: int = 2):
        """超过 N 天仍未付清的订单/用户预警。"""
        from datetime import datetime as dt, timedelta
        db = next(self.get_db())
        deadline = dt.utcnow() - timedelta(days=int(days))
        orders = db.query(Order).filter(
            Order.status != "cancelled",
            Order.payment_status != "paid",
            Order.created_at <= deadline,
        ).order_by(Order.created_at.asc()).all()
        result = []
        user_bucket = {}
        for o in orders:
            due = max(0.0, float(o.grand_total or 0) - float(o.paid_amount or 0))
            if due <= 1e-9:
                continue
            age_days = (dt.utcnow() - (o.created_at or dt.utcnow())).days
            item = {
                "order_id": o.id,
                "order_number": o.order_number,
                "user_id": o.user_id,
                "user_telegram_id": o.user.telegram_id if o.user else None,
                "user_name": (o.user.display_name or o.user.username or o.user.telegram_id) if o.user else str(o.user_id),
                "grand_total": float(o.grand_total or 0),
                "paid_amount": float(o.paid_amount or 0),
                "due_amount": round(due, 2),
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "overdue_days": age_days,
                "status": o.status,
                "payment_status": o.payment_status,
            }
            result.append(item)
            ub = user_bucket.setdefault(o.user_id, {
                "user_id": o.user_id,
                "user_name": item["user_name"],
                "user_telegram_id": item["user_telegram_id"],
                "due_amount": 0.0,
                "orders": 0,
                "max_overdue_days": 0,
            })
            ub["due_amount"] += due
            ub["orders"] += 1
            ub["max_overdue_days"] = max(ub["max_overdue_days"], age_days)
        for ub in user_bucket.values():
            ub["due_amount"] = round(ub["due_amount"], 2)
        return {
            "days": days,
            "orders": result,
            "users": sorted(user_bucket.values(), key=lambda x: -x["due_amount"]),
            "total_due": round(sum(x["due_amount"] for x in result), 2),
            "count": len(result),
        }

    def delete_warehouse(self, warehouse_id: int) -> dict:
        db = next(self.get_db())
        try:
            if db.bind.dialect.name=='sqlite':
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            wh = db.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
            if not wh:
                return {"success": False, "error": "仓库不存在"}
            # 有库存不允许硬删
            stocks = db.query(WarehouseStock).filter(WarehouseStock.warehouse_id == warehouse_id, WarehouseStock.quantity > 0).count()
            if stocks:
                return {"success": False, "error": f"仓库仍有 {stocks} 个商品有库存，请先清零或转移"}
            # Do not erase accounting/history to satisfy a warehouse deletion.
            # Check every declared FK, including extension tables, fail closed.
            connection=db.connection()
            from sqlalchemy import inspect, text
            inspector=inspect(connection)
            for table in inspector.get_table_names():
                if table=='warehouse_stock':
                    continue
                for fk in inspector.get_foreign_keys(table):
                    if fk.get('referred_table')=='warehouses':
                        for column in fk['constrained_columns']:
                            q=connection.dialect.identifier_preparer.quote
                            count=connection.execute(text(f'SELECT COUNT(*) FROM {q(table)} WHERE {q(column)}=:id'),{'id':warehouse_id}).scalar()
                            if count:
                                return {'success':False,'error':'仓库仍有关联商品、订单或库存流水，请先迁移商品；历史账务记录不能连带删除'}
            db.query(WarehouseStock).filter(WarehouseStock.warehouse_id==warehouse_id).delete(synchronize_session=False)
            db.delete(wh)
            db.commit()
            return {"success": True, "message": "仓库及空库存记录已彻底删除", "warehouse_id": warehouse_id}
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}


    @staticmethod
    def normalize_address(text: str) -> str:
        if not text:
            return ""
        s = str(text).strip().lower()
        # 去空白与常见分隔符，便于模糊匹配
        for ch in [" ", "\t", "\r", "\n", ",", "，", ".", "。", "-", "_", "/", "\\", "#", "号楼", "室"]:
            s = s.replace(ch, "")
        # 全角数字转半角
        table = str.maketrans("０１２３４５６７８９", "0123456789")
        s = s.translate(table)
        return s

    def is_address_blocked(self, address: str, session=None):
        """检查地址是否命中异常地址库。返回 (blocked: bool, matched_record_or_none)"""
        owns_session = session is None
        db = session or self.SessionLocal()
        try:
            norm = self.normalize_address(address)
            if not norm:
                return False, None
            from blocked_regions import matches,priority
            rows = db.query(BlockedAddress).filter(BlockedAddress.is_active == True).all()
            for row in sorted(rows,key=priority,reverse=True):
                if matches(row,address,self.normalize_address):return True,row
            return False, None
        finally:
            if owns_session:
                db.close()

    def list_blocked_addresses(self, active_only: bool = False, keyword: str = None, limit: int = 500):
        db = next(self.get_db())
        q = db.query(BlockedAddress)
        if active_only:
            q = q.filter(BlockedAddress.is_active == True)
        if keyword:
            kw = f"%{keyword.strip()}%"
            q = q.filter(BlockedAddress.address_text.like(kw))
        rows = q.order_by(BlockedAddress.id.desc()).limit(limit).all()
        return [{
            "id": r.id,
            "address_text": r.address_text,
            "normalized": r.normalized,
            "match_mode": r.match_mode,
            "reason": r.reason,
            "is_active": r.is_active,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]

    def add_blocked_address(self, address_text: str, reason: str = None, match_mode: str = "contains", created_by: str = "admin"):
        address_text = (address_text or "").strip()
        if not address_text:
            return {"success": False, "error": "地址不能为空"}
        db = next(self.get_db())
        try:
            from blocked_regions import validate
            validate(address_text,match_mode)
            if match_mode in {'province','city','detail'} and not (reason or '').strip():
                raise ValueError('请填写不接单原因')
            norm = self.normalize_address(address_text)
            exists = db.query(BlockedAddress).filter(BlockedAddress.normalized == norm,
                BlockedAddress.match_mode == match_mode, BlockedAddress.is_active == True).first()
            if exists:
                return {"success": False, "error": "该地址已在异常地址库中", "id": exists.id}
            row = BlockedAddress(
                address_text=address_text,
                normalized=norm,
                match_mode=(match_mode or "contains"),
                reason=reason,
                is_active=True,
                created_by=created_by,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return {"success": True, "id": row.id, "address_text": row.address_text}
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}

    def bulk_add_blocked_addresses(self, text_blob: str, reason: str = None, match_mode: str = "contains", created_by: str = "admin"):
        """支持批量粘贴，每行一个地址。"""
        lines = []
        for raw in (text_blob or "").replace("\r", "\n").split("\n"):
            line = raw.strip()
            if not line:
                continue
            # 兼容逗号分隔
            if "，" in line or "," in line:
                parts = re.split(r"[,，]", line)
                for p in parts:
                    p = p.strip()
                    if p:
                        lines.append(p)
            else:
                lines.append(line)
        added, skipped, errors = [], [], []
        for line in lines:
            res = self.add_blocked_address(line, reason=reason, match_mode=match_mode, created_by=created_by)
            if res.get("success"):
                added.append(res)
            else:
                skipped.append({"address": line, "error": res.get("error")})
        return {
            "success": True,
            "added_count": len(added),
            "skipped_count": len(skipped),
            "added": added,
            "skipped": skipped,
            "errors": errors,
        }

    def update_blocked_address(self, item_id, address_text, reason, match_mode, is_active):
        session = self.SessionLocal()
        try:
            from blocked_regions import validate
            address_text = (address_text or '').strip()
            reason = (reason or '').strip()
            if not address_text or not reason:
                raise ValueError('地址及不接单原因不能为空')
            validate(address_text, match_mode)
            row = session.get(BlockedAddress, item_id)
            if row is None:
                raise ValueError('记录不存在')
            normalized = self.normalize_address(address_text)
            duplicate = session.query(BlockedAddress).filter(
                BlockedAddress.id != item_id, BlockedAddress.normalized == normalized,
                BlockedAddress.match_mode == match_mode, BlockedAddress.is_active == True).first()
            if is_active and duplicate:
                raise ValueError('同类型有效规则已经存在')
            row.address_text, row.normalized = address_text, normalized
            row.reason, row.match_mode, row.is_active = reason, match_mode, bool(is_active)
            session.commit()
            return {'success': True, 'id': item_id}
        except Exception as exc:
            session.rollback()
            return {'success': False, 'error': str(exc)}
        finally:
            session.close()

    def delete_blocked_address(self, item_id: int, hard: bool = False):
        db = next(self.get_db())
        try:
            row = db.query(BlockedAddress).filter(BlockedAddress.id == item_id).first()
            if not row:
                return {"success": False, "error": "记录不存在"}
            if hard:
                db.delete(row)
            else:
                row.is_active = False
            db.commit()
            return {"success": True, "id": item_id}
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}


    def delete_stock_row(self, warehouse_id: int, product_id: int) -> dict:
        db = next(self.get_db())
        try:
            stock = db.query(WarehouseStock).filter(
                WarehouseStock.warehouse_id == warehouse_id,
                WarehouseStock.product_id == product_id,
            ).first()
            if not stock:
                return {"success": False, "error": "库存记录不存在"}
            qty = int(stock.quantity or 0)
            # 清零批次剩余
            batches = db.query(InventoryBatch).filter(
                InventoryBatch.warehouse_id == warehouse_id,
                InventoryBatch.product_id == product_id,
                InventoryBatch.remain_qty > 0,
            ).all()
            for b in batches:
                b.remain_qty = 0
            if qty:
                db.add(StockMovement(
                    product_id=product_id,
                    warehouse_id=warehouse_id,
                    change_qty=-qty,
                    movement_type="delete",
                    note="删除库存记录清零",
                ))
            db.delete(stock)
            self.refresh_product_stock_status(db, product_id)
            db.commit()
            return {"success": True, "message": "库存记录已删除", "cleared_qty": qty}
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}

    def daily_stats(self, day: str = None):
        """每日发货/补货/下单统计。day=YYYY-MM-DD，默认今天。"""
        from datetime import datetime as dt, timedelta
        db = next(self.get_db())
        if day:
            start = dt.strptime(day, '%Y-%m-%d')
        else:
            from business_time_range import business_now
            now = business_now()
            start = dt(now.year, now.month, now.day)
        label=start.strftime('%Y-%m-%d')
        from business_time_range import bounds
        start,end=bounds(label,label,required=True)

        # 今日订单
        orders = db.query(Order).filter(Order.created_at >= start, Order.created_at < end).all()
        shipped = db.query(Order).filter(Order.shipped_at >= start, Order.shipped_at < end).all()
        movements = db.query(StockMovement).filter(
            StockMovement.created_at >= start,
            StockMovement.created_at < end
        ).all()

        inbound = [m for m in movements if m.change_qty > 0]
        outbound = [m for m in movements if m.change_qty < 0]

        def agg_movements(items):
            bucket = {}
            for m in items:
                product = m.product
                parent = product.parent if product and product.parent else None
                key = (
                    m.product_id,
                    m.warehouse_id,
                    parent.id if parent else None,
                    parent.name if parent else '默认分类',
                    product.name if product else '',
                    m.warehouse.name if m.warehouse else '',
                )
                bucket.setdefault(key, 0)
                bucket[key] += m.change_qty
            return [{
                'product_id': k[0],
                'warehouse_id': k[1],
                'parent_id': k[2],
                'parent_name': k[3],
                'product_name': k[4],
                'warehouse_name': k[5],
                'quantity': v,
            } for k, v in bucket.items()]

        unpaid_orders = db.query(Order).filter(Order.status != 'cancelled', Order.payment_status != 'paid').all()
        total_debt = 0.0
        for o in unpaid_orders:
            total_debt += max(0, (o.grand_total or 0) - (o.paid_amount or 0))
        from manual_receivables import outstanding as manual_outstanding
        total_debt += manual_outstanding(db)

        return {
            'day': label,
            'timezone': 'Asia/Shanghai',
            'orders_created': len(orders),
            'orders_amount': round(sum(o.grand_total or 0 for o in orders if o.status != 'cancelled'), 2),
            'orders_shipped': len(shipped),
            'shipped_amount': round(sum(o.grand_total or 0 for o in shipped), 2),
            'inbound_count': len(inbound),
            'inbound_qty': sum(m.change_qty for m in inbound),
            'outbound_count': len(outbound),
            'outbound_qty': abs(sum(m.change_qty for m in outbound)),
            'inbound_items': agg_movements(inbound),
            'outbound_items': agg_movements(outbound),
            'payments_amount': round(sum(p.amount for p in db.query(Payment).filter(Payment.created_at >= start, Payment.created_at < end, Payment.method != 'prepaid').all()), 2),
            'prepaid_deduct_amount': round(sum(p.amount for p in db.query(Payment).filter(Payment.created_at >= start, Payment.created_at < end, Payment.method == 'prepaid').all()), 2),
            'total_debt': round(total_debt, 2),
        }

    # ==================== 商品父级分类相关方法 ====================


    def is_pod_product(self, product: Product) -> bool:
        """判断是否烟弹类商品（分类名/商品名含关键词）"""
        from config import SHIPPING_POD_KEYWORDS
        texts = []
        if product is not None:
            texts.append(product.name or '')
            parent_name = None
            if getattr(product, 'parent', None) is not None:
                parent_name = product.parent.name
            elif getattr(product, 'parent_id', None):
                try:
                    db = next(self.get_db())
                    parent = db.query(Product).filter(Product.id == product.parent_id).first()
                    if parent:
                        parent_name = parent.name
                except Exception:
                    parent_name = None
            if parent_name:
                texts.append(parent_name)
        blob = ' '.join(texts).lower()
        for kw in SHIPPING_POD_KEYWORDS:
            if kw and kw.lower() in blob:
                return True
        return False

    def calc_qty_shipping_fee(self, item_rows: list) -> dict:
        """
        按数量规则计算运费：
        - 烟弹类：每 N 颗 Y 元（不足按 1 段）
        - 其他：每 N 只/根 X 元（不足按 1 段）
        item_rows: [{product: Product, quantity: int}] 或含 product_name/is_pod
        """
        from config import (
            SHIPPING_FEE_AMOUNT, SHIPPING_NORMAL_QTY, SHIPPING_POD_QTY,
            SHIPPING_NORMAL_FEE_AMOUNT, SHIPPING_POD_FEE_AMOUNT,
        )
        import math
        from warehouse_shipping import partition
        all_rows=list(item_rows)
        scoped_fee, item_rows = partition(item_rows)
        pod_qty = 0
        normal_qty = 0
        details = []
        for row in item_rows:
            product = row.get('product')
            qty = int(row.get('quantity') or 0)
            if qty <= 0:
                continue
            is_pod = row.get('is_pod')
            if is_pod is None:
                from parcel_shipping import pod
                is_pod = pod(row)
            name = row.get('product_name') or (product.name if product else '')
            if is_pod:
                pod_qty += qty
                group = 'pod'
            else:
                normal_qty += qty
                group = 'normal'
            details.append({'product_name': name, 'quantity': qty, 'group': group})

        pod_segments = math.ceil(pod_qty / float(SHIPPING_POD_QTY)) if pod_qty > 0 else 0
        normal_segments = math.ceil(normal_qty / float(SHIPPING_NORMAL_QTY)) if normal_qty > 0 else 0
        normal_fee_amount = float(SHIPPING_NORMAL_FEE_AMOUNT)
        pod_fee_amount = float(SHIPPING_POD_FEE_AMOUNT)
        total_fee = scoped_fee + normal_segments * normal_fee_amount + pod_segments * pod_fee_amount
        from parcel_shipping import total as parcel_total
        total_fee=parcel_total(all_rows)
        return {
            'total_shipping_fee': float(total_fee),
            'pod_qty': pod_qty,
            'normal_qty': normal_qty,
            'pod_segments': int(pod_segments),
            'normal_segments': int(normal_segments),
            'fee_amount': float(SHIPPING_FEE_AMOUNT),
            'normal_fee_amount': normal_fee_amount,
            'pod_fee_amount': pod_fee_amount,
            'details': details,
            'rule_text': f'普通每{SHIPPING_NORMAL_QTY}只/根{normal_fee_amount:g}元；烟弹每{SHIPPING_POD_QTY}颗{pod_fee_amount:g}元',
        }

    def delete_parent_category(self, parent_id: int, move_to_default: bool = True) -> dict:
        """删除商品分类。默认把子商品迁移到默认分类，然后硬删除分类记录。"""
        db = next(self.get_db())
        try:
            parent = db.query(Product).filter(Product.id == parent_id, Product.is_parent == True).first()
            if not parent:
                return {'success': False, 'error': '分类不存在'}
            if parent.name == '默认分类':
                db.close()
                from category_lifecycle import archive
                return archive(self,parent_id,'internal')

            children = db.query(Product).filter(Product.parent_id == parent.id).all()
            moved = 0
            if children:
                if not move_to_default:
                    return {'success': False, 'error': f'分类下还有 {len(children)} 个商品，请先转移或勾选迁移到默认分类'}
                default_parent = self.get_or_create_default_parent()
                default_parent = db.query(Product).filter(Product.id == default_parent.id).first()
                for child in children:
                    child.parent_id = default_parent.id
                    moved += 1
            # 硬删除分类，避免列表里残留
            db.delete(parent)
            db.commit()
            return {
                'success': True,
                'message': f"分类已删除，迁移商品 {moved} 个" if moved else "分类已删除",
                'moved_count': moved,
                'parent_id': parent_id,
            }
        except Exception as e:
            db.rollback()
            return {'success': False, 'error': str(e)}

    def create_parent_category(self, name: str) -> Product:
        """创建父级商品分类"""
        db = next(self.get_db())
        parent = Product(name=name, is_parent=True, regular_price=0, low_stock_threshold=20)
        db.add(parent)
        db.commit()
        db.refresh(parent)
        return parent

    def get_or_create_default_parent(self) -> Product:
        """获取或创建默认父级分类"""
        db = next(self.get_db())
        parent = db.query(Product).filter(Product.name == "默认分类", Product.is_parent == True).first()
        from category_lifecycle import assert_available_default
        assert_available_default(parent)
        if not parent:
            parent = Product(name="默认分类", is_parent=True, regular_price=0, low_stock_threshold=20)
            db.add(parent)
            db.commit()
            db.refresh(parent)
        return parent

    def migrate_products_to_parent(self, default_parent_name: str = "默认分类"):
        """
        将没有父级的商品迁移到默认父级下
        这个方法只需要运行一次
        """
        db = next(self.get_db())

        # 创建默认父级
        default_parent = db.query(Product).filter(
            Product.name == default_parent_name,
            Product.is_parent == True
        ).first()
        from category_lifecycle import assert_available_default
        assert_available_default(default_parent)

        if not default_parent:
            default_parent = Product(name=default_parent_name, is_parent=True, regular_price=0, low_stock_threshold=20)
            db.add(default_parent)
            db.flush()

        # 找出所有没有父级且不是父级的商品
        products_without_parent = db.query(Product).filter(
            Product.parent_id.is_(None),
            Product.is_parent == False
        ).all()

        count = 0
        for product in products_without_parent:
            product.parent_id = default_parent.id
            count += 1

        db.commit()
        return count

    def get_all_parents(self) -> List[Product]:
        """获取所有父级分类（仅有效）"""
        db = next(self.get_db())
        return db.query(Product).filter(Product.is_parent == True, Product.is_active == True, Product.is_archived == False).order_by(Product.id.asc()).all()


    def create_product_with_parent(self, name: str, price: float, parent_id: int = None,
                                   description: str = None, **kwargs) -> Product:
        """
        创建商品（支持指定父级）
        如果没有指定父级，使用默认父级
        """
        db = next(self.get_db())

        # 如果没有指定父级，使用默认父级
        if parent_id is None:
            default_parent = self.get_or_create_default_parent()
            parent_id = default_parent.id
        selected_parent=db.query(Product).filter_by(id=parent_id,is_parent=True).first()
        if not selected_parent or selected_parent.is_archived or not selected_parent.is_active:
            raise ValueError('请选择有效分类，已删除分类不可再用于新增商品')

        kwargs.setdefault('reference_price_state', 'SET' if kwargs.get('cost_price') is not None else 'UNSET')
        product = Product(
            name=name,
            regular_price=price,
            parent_id=parent_id,
            description=description,
            **kwargs
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product


# 全局数据库实例
# Dedicated split scheduler imports mappings only, never startup migrations,
# seeding or a second global Database. Every other role keeps its old behavior.
db = (None if os.getenv('NEWBOT_ROLE') == 'split-release-only'
      and os.getenv('NEWBOT_SPLIT_MAPPINGS_ONLY') == '1' else Database())
