"""库存可售量规则。

单一职责：把「实际库存」和「预警线」换算成「可售库存」。
放在独立模块里，是因为 bot.py / config.py / models.py 在下单机器人和批发机器人
之间已经分叉，而这套规则必须两边字节一致 —— 一旦分叉，卖多卖少就会不一致。

════ 为什么需要这个模块 ════
原来预警线只是「入口闸门」：下单前检查 `当前库存 <= 预警线` 就停售。
但它挡不住已经进门的单把库存吃穿：

    库存 310，预警 300，来一单要 30 支
      → 闸门看 310 > 300，放行
      → 数量检查只看 310 >= 30，放行
      → 成交，库存变 280，预留被吃掉 20

实测批发机器人已有 13 个商品的预留被吃穿，共 1770 支。

改成「预留」语义后：可售 = 实际 − 预留。想买 810 支而可售只有 800，直接不够，
永远吃不到预留那部分。

════ 模式 ════
`STOCK_AUTO_UNLIST_MODE` 决定预警线的含义：
  threshold  预警线 = 预留量，可售 = 实际 − 预留（批发机器人 .87）
  zero       预警线不预留，可售 = 实际，库存 <= 0 才停售（下单机器人 .24）

注意：threshold 模式下「是否停售」的判据没有变化 ——
  旧：paused ⟺ 实际 <= 预警
  新：不可售 ⟺ 实际 − 预警 <= 0 ⟺ 实际 <= 预警
两者等价。所以哪些商品能卖不变，变的只是能卖多少、以及对外显示的数字。
"""

MODE_THRESHOLD = 'threshold'
MODE_ZERO = 'zero'


def normalize_mode(mode) -> str:
    """把配置值归一成两种模式之一。未知值一律按 threshold 处理。

    默认偏保守（threshold 会预留库存），因为配置写错时宁可少卖，不要超卖。
    """
    value = str(mode or '').strip().lower()
    if value in ('zero', 'stock_zero', 'order'):
        return MODE_ZERO
    return MODE_THRESHOLD


def _normalize_threshold(value) -> int:
    """预警线归一化。

    优先复用 models.normalize_low_stock_threshold —— 那是全项目的既有实现，
    空值回落到 20 而不是 0。这里绝不另写一套：预警线的含义只能有一个来源，
    否则 None 在一处算 20、另一处算 0，卖多卖少就不一致了。
    models 导入失败时才用等价的本地实现兜底。
    """
    try:
        from models import normalize_low_stock_threshold
        return normalize_low_stock_threshold(value)
    except Exception:
        if value is None:
            return 20
        if isinstance(value, str) and value.strip() == '':
            return 20
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 20


def reserved_units(threshold, mode) -> int:
    """这个商品要预留多少不出。zero 模式不预留。"""
    if normalize_mode(mode) == MODE_ZERO:
        return 0
    return _normalize_threshold(threshold)


def sellable_quantity(quantity, threshold, mode) -> int:
    """可售库存 = 实际库存 − 预留量，不会小于 0。"""
    try:
        actual = int(quantity or 0)
    except (TypeError, ValueError):
        actual = 0
    return max(0, actual - reserved_units(threshold, mode))


def is_sellable(quantity, threshold, mode, needed: int = 1) -> bool:
    """可售库存是否够 needed 个。needed 默认 1，即「这个商品还能不能卖」。"""
    try:
        want = max(1, int(needed or 1))
    except (TypeError, ValueError):
        want = 1
    return sellable_quantity(quantity, threshold, mode) >= want


def stock_view(quantity, threshold, mode) -> dict:
    """给后台用的三个数：实际 / 可售 / 预留。"""
    reserved = reserved_units(threshold, mode)
    return {
        'actual_quantity': max(0, int(quantity or 0)),
        'sellable_quantity': sellable_quantity(quantity, threshold, mode),
        'reserved_quantity': reserved,
    }


def shortage_text(product_name: str, quantity, threshold, mode, needed: int) -> str:
    """库存不足时给客户/仓库看的说明。只暴露可售量，不暴露预留细节。"""
    sellable = sellable_quantity(quantity, threshold, mode)
    return (
        f'{product_name} 可售库存不足（可售: {sellable}, 需要: {needed}）'
    )
