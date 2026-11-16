# -*- coding: utf-8 -*-
"""订单数量与自动分单规则。**单一实现**，bot 和 API 共用。

当前自动录单规则是一条消息生成一张正式订单，每 6 支一个物理包裹；
历史分单工具仍保留兼容旧订单及后台流程。历史的数量/地址检查函数仍保留供
旧配置和回归测试使用，当前运行配置将两项上限固定为 0（不限制）。

数量一律按支数计算，与运费分段规则分开。
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 0 表示不限制 / 不拆包
FIELDS = ('max_units_per_order', 'units_per_package', 'address_daily_max_units')


def _int(value: Any, default: int = 0) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else 0


def normalize(section: Optional[Dict], defaults: Optional[Dict] = None) -> Dict[str, int]:
    """把 settings.json 的 packaging 段规整成三个非负整数。"""
    data = section if isinstance(section, dict) else {}
    base = defaults if isinstance(defaults, dict) else {}
    out = {}
    for field in FIELDS:
        raw = data.get(field, base.get(field, 0))
        out[field] = _int(raw, _int(base.get(field, 0)))
    return out


def total_units(items: Iterable) -> int:
    """订单总支数。item 可以是 dict 也可以是 ORM 对象。"""
    total = 0
    for item in items or []:
        if isinstance(item, dict):
            quantity = item.get('quantity')
        else:
            quantity = getattr(item, 'quantity', 0)
        total += max(0, _int(quantity))
    return total


def package_count(units: int, units_per_package: int) -> int:
    """支数 -> 包裹数；与商品行、口味及分类数量无关。"""
    units = max(0, _int(units))
    size = _int(units_per_package)
    if units <= 0:
        return 0
    if size <= 0:
        return 1
    return (units + size - 1) // size


def final_package_count(items: Iterable) -> int:
    """Same configured physical parcels as the shipping quote."""
    from parcel_shipping import plan
    rows=[item if isinstance(item,dict) else dict(quantity=item.quantity,
          product=item.product,warehouse_id=item.warehouse_id) for item in items]
    return len(plan(rows))


def check_units(units: int, limits: Dict[str, int]) -> Tuple[bool, str]:
    """单笔订单数量闸门。返回 (是否放行, 给客户看的原因)。"""
    units = max(0, _int(units))
    cap = _int((limits or {}).get('max_units_per_order'))
    if cap and units > cap:
        size = _int((limits or {}).get('units_per_package'))
        detail = f'（快递限制：单个地址一天最多 {cap} 支'
        if size:
            detail += f'，每 {size} 支一个包裹'
        detail += '）'
        return False, (
            f'❌ 这一单 {units} 支，超过单笔上限 {cap} 支，暂时下不了单。\n'
            f'{detail}\n请把数量改到 {cap} 支以内再发一次 🙏'
        )
    return True, ''


def check_address_daily(
    units: int,
    used_units: int,
    limits: Dict[str, int],
) -> Tuple[bool, str]:
    """同一地址当天累计闸门。used_units 是这个地址今天已经下过的支数。"""
    units = max(0, _int(units))
    used = max(0, _int(used_units))
    cap = _int((limits or {}).get('address_daily_max_units'))
    if not cap:
        return True, ''
    if used + units <= cap:
        return True, ''
    remaining = max(0, cap - used)
    if remaining <= 0:
        return False, (
            f'❌ 这个收货地址今天已经下了 {used} 支，达到快递上限 {cap} 支了。\n'
            '同一个地址一天发太多会被快递集包扣件，麻烦明天再下，或者换个收货地址 🙏'
        )
    return False, (
        f'❌ 这个收货地址今天已经下了 {used} 支，今天最多还能下 {remaining} 支。\n'
        f'（快递限制：单个地址一天最多 {cap} 支）\n'
        f'麻烦把数量改成 {remaining} 支以内，或者明天再下 🙏'
    )


class PackageItem:
    """拆包后喂给风火递的轻量 item 影子。

    `fhd_client.build_order_info_from_local` / `build_merchant_trade_from_local`
    只取 `.product` / `.product_id` / `.quantity` / `.unit_price`，
    所以不需要真的 ORM 对象——真对象拆包会改动数据库里的数量。
    """

    __slots__ = ('product', 'product_id', 'quantity', 'unit_price', 'source')

    def __init__(self, item, quantity: int):
        self.product = getattr(item, 'product', None)
        self.product_id = getattr(item, 'product_id', None)
        self.quantity = int(quantity)
        self.unit_price = getattr(item, 'unit_price', 0)
        self.source = item

    def __repr__(self):
        return f'PackageItem(product_id={self.product_id!r}, quantity={self.quantity})'


def split_into_packages(items: Sequence, units_per_package: int) -> List[List[PackageItem]]:
    """按支数把订单拆成若干包裹。

    单个商品数量大于一包容量时会被拆开（每包6支时，7支 → 6 + 1），所以拆的粒度是「支」不是「行」。
    `units_per_package <= 0` 表示不拆，整单一个包裹。
    """
    size = _int(units_per_package)
    rows = []
    for item in items or []:
        quantity = max(0, _int(getattr(item, 'quantity', 0)))
        if quantity > 0:
            rows.append([item, quantity])
    if not rows:
        return []
    if size <= 0:
        return [[PackageItem(item, quantity) for item, quantity in rows]]

    packages: List[List[PackageItem]] = []
    current: List[PackageItem] = []
    space = size
    for row in rows:
        item, remaining = row
        while remaining > 0:
            take = min(space, remaining)
            current.append(PackageItem(item, take))
            remaining -= take
            space -= take
            if space == 0:
                packages.append(current)
                current = []
                space = size
    if current:
        packages.append(current)
    return packages


def split_dict_items(items: Sequence[Dict[str, Any]], units_per_order: int) -> List[List[Dict[str, Any]]]:
    """按数量切分字典商品行，并保留原字段。

    与 :func:`split_into_packages` 的差别是这里用于创建正式子订单：每个返回行
    都是独立字典，修改 ``quantity`` 不会污染调用方的原始清单。
    """
    size = _int(units_per_order)
    rows: List[Tuple[Dict[str, Any], int]] = []
    for item in items or []:
        quantity = max(0, _int((item or {}).get('quantity')))
        if quantity:
            rows.append((dict(item), quantity))
    if not rows:
        return []
    if size <= 0:
        return [[{**item, 'quantity': quantity} for item, quantity in rows]]

    result: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    space = size
    for item, remaining in rows:
        while remaining > 0:
            take = min(space, remaining)
            current.append({**item, 'quantity': take})
            remaining -= take
            space -= take
            if space == 0:
                result.append(current)
                current = []
                space = size
    if current:
        result.append(current)
    return result


def normalize_category_limits(value: Any) -> Dict[str, int]:
    """规整 ``{父级商品ID: 每笔数量}``，忽略无效键和值。"""
    if not isinstance(value, dict):
        return {}
    result: Dict[str, int] = {}
    for raw_key, raw_limit in value.items():
        key = str(raw_key or '').strip()
        limit = _int(raw_limit)
        if key and limit > 0:
            result[key] = limit
    return result


def split_dict_items_by_category(
    items: Sequence[Dict[str, Any]],
    default_units_per_order: int,
    category_limits: Optional[Dict[str, int]] = None,
) -> List[List[Dict[str, Any]]]:
    """按父级品类分别切分正式子订单。

    ``category_id``（或商品对象的 ``parent_id``）相同的行属于同一品类。
    每个品类先使用自己的限制，未配置时使用全局默认值。不同品类不会被塞进
    同一个子单，便于仓库按品类规则独立核对和打印。
    """
    default_size = _int(default_units_per_order, 6) or 6
    limits = normalize_category_limits(category_limits)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []

    for raw_item in items or []:
        item = dict(raw_item or {})
        quantity = max(0, _int(item.get('quantity')))
        if not quantity:
            continue
        product = item.get('product')
        category_id = item.get('category_id')
        if category_id in (None, '') and product is not None:
            category_id = getattr(product, 'parent_id', None)
        # 无父级的历史商品各自作为独立规则组，避免与其他未分类商品误混。
        key = str(category_id) if category_id not in (None, '') else f"product:{item.get('product_id', len(order))}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        item['quantity'] = quantity
        item['category_id'] = category_id
        grouped[key].append(item)

    packages: List[List[Dict[str, Any]]] = []
    for key in order:
        size = limits.get(key, default_size)
        packages.extend(split_dict_items(grouped[key], size))
    return packages


def package_count_by_category(
    items: Sequence[Dict[str, Any]],
    default_units_per_order: int,
    category_limits: Optional[Dict[str, int]] = None,
) -> int:
    """返回按品类规则拆分后的正式子单数量。"""
    return len(split_dict_items_by_category(items, default_units_per_order, category_limits))


# --------------------------------------------------------------------------
# 当天累计用的地址键
# --------------------------------------------------------------------------
_ADDR_NOISE = re.compile(r'[\s,，。．.、;；:：!！?？~～\-—_()（）\[\]【】"\'"　]+')
_ADDR_PREFIX = re.compile(r'^(收货地址|地址|收件地址)[:：]?')


def address_key(address: Optional[str]) -> str:
    """同一地址当天累计的键。

    只做「去噪声 + 归一化行政区」，不做模糊匹配：宁可漏卡也不要把两个不同地址
    算成一个（算错会让本来能下的单下不了）。
    写法差异太大的同一地址匹配不上 —— 这是已知取舍，见 task_plan。
    """
    text = str(address or '').strip()
    if not text:
        return ''
    text = _ADDR_PREFIX.sub('', text)
    try:
        from address_utils import normalize_cn_address
        parsed = normalize_cn_address(text) or {}
        # Resolve a suffixless district only from the existing administrative
        # index and a known province. Never fuzzy-match the door/room detail.
        if parsed.get('province') and not parsed.get('district'):
            from region_index import get_region_index
            from address_utils import MUNICIPALITIES, PROVINCE_ALIASES
            index=get_region_index(); candidates=[]
            detail=str(parsed.get('detail') or '')
            for name,nodes in index.region_by_name.items():
                for alias in index._short_region_names(name):
                    if not detail.startswith(alias):continue
                    for node in nodes:
                        chain=index._chains[node.id]
                        province=MUNICIPALITIES.get(chain.province,PROVINCE_ALIASES.get(chain.province,chain.province))
                        if province==parsed['province'] and (not parsed.get('city') or parsed['city']=='市辖区' or chain.city==parsed['city']):
                            candidates.append((name,alias))
            if len(set(candidates))==1:
                name,alias=candidates[0];parsed['district']=name;parsed['detail']=detail[len(alias):]
        parts = [
            str(parsed.get('province') or ''),
            str(parsed.get('city') or ''),
            str(parsed.get('district') or ''),
            str(parsed.get('town') or ''),
            str(parsed.get('detail') or ''),
        ]
        joined = ''.join(parts).strip()
        if joined:
            text = joined
    except Exception:
        pass
    return _ADDR_NOISE.sub('', text).casefold()


def describe(limits: Dict[str, int]) -> str:
    """给后台/日志用的一句话说明。"""
    cap = _int((limits or {}).get('max_units_per_order'))
    size = _int((limits or {}).get('units_per_package'))
    daily = _int((limits or {}).get('address_daily_max_units'))
    if not any((cap, size, daily)):
        return '未启用数量/包裹限制'
    bits = []
    if cap:
        bits.append(f'单笔最多 {cap} 支')
    if size:
        bits.append(f'每 {size} 支一个包裹')
    if daily:
        bits.append(f'同一地址当天最多 {daily} 支')
    return '；'.join(bits)


# --------------------------------------------------------------------------
# 「当天」的边界
# --------------------------------------------------------------------------
# 服务器时区是 UTC，`Order.created_at` 存的是 `datetime.utcnow()`。但快递说的
# 「一天」是北京时间的一天。直接拿 UTC 日切会错 8 小时：北京时间早上 7 点下的单
# 会被算进前一天，于是同一个北京日可以下两轮 10 支 —— 正是要防的那种情况。
DAY_OFFSET_HOURS = 8


def day_window_utc(now_utc=None, offset_hours: int = DAY_OFFSET_HOURS):
    """返回「本地自然日」对应的 UTC 起止时间，用来筛 created_at。"""
    from datetime import datetime, timedelta
    now_utc = now_utc or datetime.utcnow()
    offset = timedelta(hours=offset_hours)
    local_now = now_utc + offset
    local_start = datetime(local_now.year, local_now.month, local_now.day)
    start_utc = local_start - offset
    return start_utc, start_utc + timedelta(days=1)


def local_day_str(now_utc=None, offset_hours: int = DAY_OFFSET_HOURS) -> str:
    from datetime import datetime, timedelta
    now_utc = now_utc or datetime.utcnow()
    return (now_utc + timedelta(hours=offset_hours)).strftime('%Y-%m-%d')
