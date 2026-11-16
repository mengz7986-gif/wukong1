"""
库存图片识别模块
支持从Excel表格截图中识别库存信息并更新到数据库
"""

import os
import re
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class InventoryOCR:
    """库存图片识别器"""

    def __init__(self, db_session=None):
        """
        初始化识别器
        Args:
            db_session: 数据库会话（用于模糊匹配商品名称）
        """
        self.db = db_session

    def parse_text_to_inventory(self, text: str, warehouse_name: str = None) -> List[Dict]:
        """
        将OCR识别的文本解析为库存数据
        Args:
            text: OCR识别的文本
            warehouse_name: 指定的仓库名称（如果为None，则尝试从文本中识别）

        Returns:
            解析后的库存数据列表 [{"product_name": "...", "quantity": ..., "warehouse_name": "..."}, ...]
        """
        inventory_items = []
        lines = text.strip().split('\n')

        current_parent = None  # 当前父级商品（如"可乐"）

        for line in lines:
            line = line.strip()
            if not line or len(line) < 3:
                continue

            # 跳过明显是标题的行
            if self._is_header_line(line):
                continue

            # 尝试从行中提取商品名和数量
            item = self._parse_line(line, current_parent)
            if item:
                # 如果指定了仓库，使用指定的
                if warehouse_name:
                    item['warehouse_name'] = warehouse_name

                # 判断是否是父级商品（没有明显的子特征）
                if self._is_parent_product(line):
                    current_parent = item['product_name']
                    item['is_parent'] = True
                else:
                    item['is_parent'] = False
                    # 如果有父级，组合完整名称
                    if current_parent:
                        full_name = f"{current_parent}-{item['product_name']}"
                        item['full_product_name'] = full_name

                inventory_items.append(item)

        return inventory_items

    def _is_header_line(self, line: str) -> bool:
        """判断是否是标题行"""
        header_keywords = ['商品', '产品', '名称', '规格', '数量', '库存', '单位', 'price', 'product', 'name']
        line_lower = line.lower()
        return any(keyword in line_lower for keyword in header_keywords)

    def _is_parent_product(self, line: str) -> bool:
        """
        判断是否是父级商品
        父级商品通常：没有详细规格描述、行较短、可能是大类名称
        """
        # 如果行中包含常见规格词，可能是子商品
        child_keywords = ['味', 'ml', 'ML', 'g', 'KG', 'kg', '升', 'l', 'L', '装', '包', '瓶', '罐']
        is_child = any(keyword in line for keyword in child_keywords)

        # 行长度较短且没有子商品特征，可能是父级
        if not is_child and len(line) < 20:
            return True

        return False

    def _parse_line(self, line: str, parent_name: str = None) -> Optional[Dict]:
        """
        解析单行文本，提取商品名和数量
        支持多种格式：
        - 商品名 数量
        - 商品名 x数量
        - 商品名 : 数量
        - 数字结尾（数字即数量）
        """
        # 尝试多种模式匹配

        # 模式1: "商品名 x 数量" 或 "商品名 * 数量"
        pattern1 = r'(.+?)[x*×]\s*(\d+)'
        match = re.search(pattern1, line, re.IGNORECASE)
        if match:
            product_name = match.group(1).strip()
            quantity = int(match.group(2))
            return self._create_item(product_name, quantity, line)

        # 模式2: "商品名 : 数字" (带分隔符)
        pattern2 = r'(.+?)[:：\t|]\s*(\d+)'
        match = re.search(pattern2, line)
        if match:
            name_part = match.group(1).strip()
            quantity = int(match.group(2))
            # 过滤掉明显是标题的
            if not self._is_header_line(name_part):
                return self._create_item(name_part, quantity, line)

        # 模式3: 行尾是数字（数量）
        pattern3 = r'^(.+?)\s+(\d+)\s*$'
        match = re.search(pattern3, line)
        if match:
            name_part = match.group(1).strip()
            quantity = int(match.group(2))
            # 名称部分不能太短，避免误识别
            if len(name_part) >= 2 and not self._is_header_line(name_part):
                return self._create_item(name_part, quantity, line)

        # 模式4: Excel表格中的制表符分隔
        if '\t' in line:
            parts = line.split('\t')
            # 找商品名（第一个非空非标题列）
            product_name = None
            quantity = None

            for i, part in enumerate(parts):
                part = part.strip()
                if not part:
                    continue
                if self._is_header_line(part):
                    continue
                if product_name is None:
                    product_name = part
                elif part.isdigit() and quantity is None:
                    quantity = int(part)
                    break

            if product_name and quantity is not None:
                return self._create_item(product_name, quantity, line)

        return None

    def _create_item(self, product_name: str, quantity: int, original_line: str) -> Dict:
        """创建库存项"""
        # 清理商品名称
        product_name = product_name.strip()
        # 移除常见的干扰字符
        product_name = re.sub(r'^[\d\.\s]+', '', product_name)  # 开头的数字
        product_name = re.sub(r'[【】\[\]\(\)\{\}]', '', product_name)  # 各种括号
        product_name = product_name.strip()

        return {
            'product_name': product_name,
            'quantity': quantity,
            'original_line': original_line,
            'warehouse_name': None,
            'full_product_name': None,
            'is_parent': False
        }

    def fuzzy_match_product(self, product_name: str, all_products: List[Dict]) -> Optional[Dict]:
        """
        模糊匹配商品名称
        Args:
            product_name: 待匹配的商品名称
            all_products: 所有商品列表 [{"id": ..., "name": ...}, ...]

        Returns:
            匹配到的商品信息，如果没有匹配返回None
        """
        if not all_products:
            return None

        # 首先尝试精确匹配
        for product in all_products:
            if product['name'] == product_name:
                return product

        # 模糊匹配（相似度 > 0.6）
        best_match = None
        best_ratio = 0.6

        for product in all_products:
            ratio = SequenceMatcher(None, product_name.lower(), product['name'].lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = product

        # 如果是子商品，尝试组合父级名称后匹配
        if '-' in product_name:
            parts = product_name.split('-', 1)
            parent = parts[0].strip()
            child = parts[1].strip()

            # 尝试 "父级-子级" 格式
            combined = f"{parent}-{child}"
            for product in all_products:
                if product['name'] == combined:
                    return product

            # 尝试只匹配子级部分
            for product in all_products:
                if child in product['name'] or product['name'] in child:
                    ratio = SequenceMatcher(None, child, product['name']).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = product

        return best_match

    def match_warehouse(self, warehouse_name: str, all_warehouses: List[Dict]) -> Optional[Dict]:
        """
        匹配仓库
        Args:
            warehouse_name: 仓库名称
            all_warehouses: 所有仓库列表 [{"id": ..., "name": ...}, ...]

        Returns:
            匹配到的仓库信息，如果没有匹配返回None
        """
        if not warehouse_name or not all_warehouses:
            return None

        # 精确匹配
        for warehouse in all_warehouses:
            if warehouse['name'] == warehouse_name:
                return warehouse

        # 模糊匹配
        for warehouse in all_warehouses:
            if warehouse_name in warehouse['name'] or warehouse['name'] in warehouse_name:
                return warehouse

        return None


class InventoryUpdateResult:
    """库存更新结果"""

    def __init__(self):
        self.success_count = 0
        self.failed_count = 0
        self.items = []  # [{"product_name": ..., "action": "updated/created/skipped", "message": "..."}, ...]
        self.warehouse_name = None
        self.timestamp = datetime.now()

    def to_dict(self):
        return {
            'success_count': self.success_count,
            'failed_count': self.failed_count,
            'items': self.items,
            'warehouse_name': self.warehouse_name,
            'timestamp': self.timestamp.isoformat()
        }


class InventoryUpdater:
    """库存更新器"""

    def __init__(self, db):
        """
        初始化更新器
        Args:
            db: Database实例
        """
        self.db = db
        self.ocr = InventoryOCR(db)

    def update_from_text(self, text: str, warehouse_name: str = None,
                         incremental: bool = True) -> InventoryUpdateResult:
        """
        从文本更新库存
        Args:
            text: OCR识别的文本
            warehouse_name: 仓库名称
            incremental: 是否增量更新（True=加到现有库存，False=覆盖库存）

        Returns:
            InventoryUpdateResult
        """
        result = InventoryUpdateResult()
        result.warehouse_name = warehouse_name

        # 获取所有商品和仓库
        from models import Product, Warehouse, WarehouseStock
        db_session = next(self.db.get_db())
        all_products = [{'id': p.id, 'name': p.name}
                        for p in db_session.query(Product)
                        .filter(Product.is_active == True).all()]
        all_warehouses = [{'id': w.id, 'name': w.name}
                          for w in db_session.query(Warehouse)
                          .filter(Warehouse.is_active == True).all()]

        # 匹配仓库
        matched_warehouse = None
        if warehouse_name:
            matched_warehouse = self.ocr.match_warehouse(warehouse_name, all_warehouses)
        elif all_warehouses:
            matched_warehouse = all_warehouses[0]  # 默认使用第一个仓库

        if not matched_warehouse:
            result.failed_count = 1
            result.items.append({
                'product_name': 'N/A',
                'action': 'error',
                'message': f'仓库 "{warehouse_name}" 不存在'
            })
            return result

        warehouse_id = matched_warehouse['id']
        result.warehouse_name = matched_warehouse['name']

        # 解析库存项
        inventory_items = self.ocr.parse_text_to_inventory(text, matched_warehouse['name'])

        for item in inventory_items:
            product_name = item.get('full_product_name') or item['product_name']
            quantity = item['quantity']

            # 匹配商品
            matched_product = self.ocr.fuzzy_match_product(product_name, all_products)

            if not matched_product:
                result.failed_count += 1
                result.items.append({
                    'product_name': product_name,
                    'quantity': quantity,
                    'action': 'skipped',
                    'message': '商品未找到'
                })
                continue

            # 更新库存
            product_id = matched_product['id']
            stock = db_session.query(WarehouseStock).filter(
                WarehouseStock.warehouse_id == warehouse_id,
                WarehouseStock.product_id == product_id
            ).first()

            old_quantity = 0
            if stock:
                old_quantity = stock.quantity
                if incremental:
                    stock.quantity += quantity
                else:
                    stock.quantity = quantity
            else:
                if incremental:
                    new_quantity = quantity
                else:
                    new_quantity = quantity
                stock = WarehouseStock(
                    warehouse_id=warehouse_id,
                    product_id=product_id,
                    quantity=new_quantity
                )
                db_session.add(stock)

            try:
                db_session.commit()
                result.success_count += 1
                action = 'updated' if old_quantity > 0 else 'created'
                result.items.append({
                    'product_name': matched_product['name'],
                    'original_name': product_name,
                    'quantity': quantity,
                    'old_quantity': old_quantity,
                    'new_quantity': stock.quantity,
                    'action': action,
                    'message': f'库存从 {old_quantity} 更新到 {stock.quantity}'
                })
                logger.info(f"更新库存: {matched_product['name']} {old_quantity} -> {stock.quantity}")
            except Exception as e:
                db_session.rollback()
                result.failed_count += 1
                result.items.append({
                    'product_name': product_name,
                    'quantity': quantity,
                    'action': 'error',
                    'message': str(e)
                })
                logger.error(f"更新库存失败: {product_name}, 错误: {e}")

        return result


def create_demo_inventory_text():
    """创建演示用的库存文本"""
    return """
    商品名称        数量
    可乐           100
    原味           50
    樱桃味         30
    雪碧           80
    柠檬味         40
    矿泉水         200
    """


if __name__ == "__main__":
    # 测试代码
    from models import Database

    db = Database()
    updater = InventoryUpdater(db)

    # 测试文本
    test_text = """
    商品名称    数量
    苹果       50
    香蕉       30
    橙子       40
    """

    result = updater.update_from_text(test_text, warehouse_name="默认仓库", incremental=True)

    print(f"成功: {result.success_count}, 失败: {result.failed_count}")
    for item in result.items:
        print(f"  {item}")
