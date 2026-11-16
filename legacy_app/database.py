"""
[DEPRECATED] 旧 JSON 数据库（已废弃，请使用 models.py/SQLite）
保留仅用于兼容参考，主流程不要再调用本文件。
"""
import json
import os
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from config import PRODUCTS, INITIAL_STOCK, INITIAL_BALANCES


class Database:
    def __init__(self, db_file: str = "data.json"):
        self.db_file = db_file
        self.data = self._load_data()

    def _load_data(self) -> Dict:
        """加载数据库"""
        if os.path.exists(self.db_file):
            with open(self.db_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            # 初始化数据库
            return {
                "users": {},
                "stock": INITIAL_STOCK.copy(),
                "orders": []
            }

    def _save_data(self):
        """保存数据库"""
        with open(self.db_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_product_price(self, product_name: str) -> Optional[float]:
        """获取商品价格"""
        return PRODUCTS.get(product_name)

    def get_product_list(self) -> Dict[str, float]:
        """获取商品列表"""
        return PRODUCTS.copy()

    def get_stock(self, product_name: str) -> int:
        """获取商品库存"""
        return self.data["stock"].get(product_name, 0)

    def get_all_stock(self) -> Dict[str, int]:
        """获取所有商品库存"""
        return self.data["stock"].copy()

    def check_stock(self, items: List[Tuple[str, int]]) -> Tuple[bool, List[str]]:
        """
        检查库存是否充足
        Returns: (是否充足, 不足的商品列表)
        """
        insufficient = []
        for product_name, quantity in items:
            stock = self.get_stock(product_name)
            if stock < quantity:
                insufficient.append(f"{product_name} (库存: {stock}, 需要: {quantity})")
        return len(insufficient) == 0, insufficient

    def update_stock(self, items: List[Tuple[str, int]], decrease: bool = True):
        """
        更新库存
        Args:
            items: [(商品名, 数量), ...]
            decrease: True为减少库存，False为增加库存
        """
        for product_name, quantity in items:
            current_stock = self.get_stock(product_name)
            if decrease:
                self.data["stock"][product_name] = max(0, current_stock - quantity)
            else:
                self.data["stock"][product_name] = current_stock + quantity
        self._save_data()

    def get_user_balance(self, user_id: str) -> float:
        """获取用户余额"""
        if user_id not in self.data["users"]:
            self.data["users"][user_id] = {
                "balance": INITIAL_BALANCES.get(user_id, 100.0),
                "orders": []
            }
            self._save_data()
        return self.data["users"][user_id]["balance"]

    def update_balance(self, user_id: str, amount: float) -> bool:
        """
        更新用户余额
        Returns: 是否成功
        """
        current_balance = self.get_user_balance(user_id)
        if current_balance + amount < 0:
            return False

        self.data["users"][user_id]["balance"] = current_balance + amount
        self._save_data()
        return True

    def create_order(self, user_id: str, items: List[Tuple[str, int]],
                     address: str, recipient: str, phone: str) -> Optional[Dict]:
        """
        创建订单
        Returns: 订单信息或None（如果余额不足）
        """
        # 计算总价
        total_price = 0
        for product_name, quantity in items:
            price = self.get_product_price(product_name)
            if price:
                total_price += price * quantity

        # 检查余额
        balance = self.get_user_balance(user_id)
        if balance < total_price:
            return None

        # 扣除余额
        if not self.update_balance(user_id, -total_price):
            return None

        # 减少库存
        self.update_stock(items, decrease=True)

        # 创建订单
        order_id = len(self.data["orders"]) + 1
        order = {
            "id": order_id,
            "user_id": user_id,
            "items": items,
            "total_price": total_price,
            "address": address,
            "recipient": recipient,
            "phone": phone,
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }

        self.data["orders"].append(order)
        self.data["users"][user_id]["orders"].append(order_id)
        self._save_data()

        return order

    def get_user_orders(self, user_id: str) -> List[Dict]:
        """获取用户订单历史"""
        user_data = self.data["users"].get(user_id)
        if not user_data:
            return []

        order_ids = user_data.get("orders", [])
        return [order for order in self.data["orders"] if order["id"] in order_ids]

    def get_order_by_id(self, order_id: int) -> Optional[Dict]:
        """根据ID获取订单"""
        for order in self.data["orders"]:
            if order["id"] == order_id:
                return order
        return None

    def update_order_status(self, order_id: int, status: str):
        """更新订单状态"""
        order = self.get_order_by_id(order_id)
        if order:
            order["status"] = status
            self._save_data()

    def add_balance(self, user_id: str, amount: float) -> bool:
        """给用户充值"""
        return self.update_balance(user_id, amount)


# 全局数据库实例
db = Database()
