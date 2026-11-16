"""缺货策略引擎。

纯函数 + 可注入库存查询，便于在没有真实库存的环境里用构造数据单测。
不 import bot / api_client，避免循环依赖。

策略来自客户原话里的发货指令，由 DeepSeek 结构化成 Directive：
  alternate    备选替换：主口味缺货时换客户指定的备选
  random       缺货随机：从同品类有货的里自动挑
  cancel_line  缺货退单：缺货就删掉该行
  exact        精准发货 / 只发X：禁止任何替换，缺货即删
  none         没有指令：缺货按现有行为报错，整单中止

「随机」实现为**按库存量降序取第一个**而不是真随机：真随机会让 bug 不可复现，
而且优先出库存多的对周转更好。对客户而言两者都满足「你随便发」的语义。
"""

from typing import Callable, Dict, List, Optional, Sequence, Tuple

POLICY_NONE = 'none'
POLICY_ALTERNATE = 'alternate'
POLICY_RANDOM = 'random'
POLICY_REPEAT_SELECTED = 'repeat_selected'
POLICY_CANCEL_LINE = 'cancel_line'
POLICY_EXACT = 'exact'

VALID_POLICIES = (
    POLICY_NONE, POLICY_ALTERNATE, POLICY_RANDOM, POLICY_REPEAT_SELECTED,
    POLICY_CANCEL_LINE, POLICY_EXACT,
)

# 允许自动放行的策略。none 之外都意味着「客户已经说了缺货怎么办」，
# 所以不需要人工再确认一次。
AUTO_POLICIES = (POLICY_ALTERNATE, POLICY_RANDOM, POLICY_REPEAT_SELECTED, POLICY_CANCEL_LINE, POLICY_EXACT)


class Directive:
    """一张订单的发货指令。"""

    def __init__(
        self,
        on_out_of_stock: str = POLICY_NONE,
        alternates: Optional[Dict[str, str]] = None,
        exact_only: Optional[Sequence[str]] = None,
        is_aftersale: bool = False,
        selection_scope: str = '',
    ):
        self.on_out_of_stock = (
            on_out_of_stock if on_out_of_stock in VALID_POLICIES else POLICY_NONE
        )
        # 主口味 -> 备选口味，都是目录规范名
        self.alternates = dict(alternates or {})
        self.exact_only = [str(x).strip() for x in (exact_only or []) if str(x).strip()]
        self.is_aftersale = bool(is_aftersale)
        self.selection_scope = selection_scope if selection_scope == 'selected_mutual' else ''

    @property
    def is_automatic(self) -> bool:
        return self.on_out_of_stock in AUTO_POLICIES

    def __repr__(self):
        return (
            f'Directive(on_out_of_stock={self.on_out_of_stock!r}, '
            f'alternates={self.alternates!r}, exact_only={self.exact_only!r}, '
            f'is_aftersale={self.is_aftersale!r})'
        )


def from_ai_result(ai_result: Dict) -> Directive:
    """从 DeepSeek 输出里取出结构化指令。

    只读 fulfillment 结构化字段和 alternates，**不去正则匹配 notes 中文文本**——
    自由文本匹配脆且不可穷举，语义识别是模型的活。
    """
    raw = ai_result.get('fulfillment') or {}
    if not isinstance(raw, dict):
        raw = {}

    alternates: Dict[str, str] = {}
    for entry in ai_result.get('alternates') or []:
        if not isinstance(entry, dict):
            continue
        main = str(entry.get('for') or '').strip()
        substitute = str(entry.get('flavor') or '').strip()
        if main and substitute:
            current=alternates.setdefault(main,[])
            if substitute not in current:current.append(substitute)

    policy = str(raw.get('on_out_of_stock') or '').strip() or POLICY_NONE
    # 客户写了备选但模型没标策略时，兜底认定为备选替换
    if policy == POLICY_NONE and alternates:
        policy = POLICY_ALTERNATE

    return Directive(
        on_out_of_stock=policy,
        alternates=alternates,
        exact_only=raw.get('exact_only') or [],
        is_aftersale=bool(raw.get('is_aftersale')),
        selection_scope=raw.get('selection_scope',''),
    )


def from_order_info(order_info: Dict) -> Directive:
    """从 to_order_info 产出的 order_info 里重建 Directive。

    order_info 会在 bot 内部传递多层，所以只存普通 dict，到用的时候再重建对象。
    """
    raw = order_info.get('ai_fulfillment') or {}
    if not isinstance(raw, dict):
        raw = {}
    alternates: Dict[str, str] = {}
    for entry in order_info.get('ai_alternates') or []:
        if not isinstance(entry, dict):
            continue
        main = str(entry.get('for') or '').strip()
        substitute = str(entry.get('flavor') or '').strip()
        if main and substitute:
            current=alternates.setdefault(main,[])
            if substitute not in current:current.append(substitute)
    policy = str(raw.get('on_out_of_stock') or '').strip() or POLICY_NONE
    if policy == POLICY_NONE and alternates:
        policy = POLICY_ALTERNATE
    return Directive(
        on_out_of_stock=policy,
        alternates=alternates,
        exact_only=raw.get('exact_only') or [],
        is_aftersale=bool(raw.get('is_aftersale')),
        selection_scope=raw.get('selection_scope',''),
    )


def _available(
    stock: int,
    threshold: int,
    needed: int,
    is_paused: Callable[[int, int], bool],
) -> bool:
    """是否可发：商品未被停售，且库存够本次数量。

    停售判断必须由调用方注入 `bot.is_stock_paused`，**不能在这里自己实现**。
    它的语义受 `STOCK_AUTO_UNLIST_MODE` 控制：
      - `zero`（线上 .24 当前配置）：库存 <= 0 才停售
      - `threshold`：库存 <= 预警线就停售
    自己复制一份逻辑的下场是：mode=zero 时把「库存 5 / 预警 20」误判成缺货，
    于是在客户主口味其实有货的情况下替换成备选 —— 直接发错货。
    """
    stock = max(0, int(stock or 0))
    threshold = max(0, int(threshold or 0))
    if is_paused(stock, threshold):
        return False
    return stock >= needed


def plan_items(
    items: Sequence[Dict],
    directive: Directive,
    products: Sequence[Dict],
    stock_of: Callable[[int], int],
    normalize: Callable[[Optional[str]], str],
    is_paused: Callable[[int, int], bool],
) -> Tuple[List[Dict], List[str], List[str]]:
    """按策略把订单行排成最终可发清单。

    items 每项需含 category / flavor / quantity（目录规范名），可选 raw_flavor。
    stock_of(product_id) -> 可用库存，由调用方注入（线上查 API，测试给构造数据）。
    is_paused(stock, threshold) -> 是否停售，必须传 bot.is_stock_paused，见 _available。

    返回 (最终清单, 动作说明, 错误)。错误非空即整单不能自动下。
    """
    by_key = {}
    by_id = {}
    for product in products:
        key = (normalize(product.get('parent_name')), normalize(product.get('name')))
        by_key.setdefault(key, []).append(product)
        by_id[product.get('id')] = product

    def lookup(category, flavor):
        candidates = by_key.get((normalize(category), normalize(flavor)), [])
        # 仓库记录不是不同业务商品。这里只选一条实际可用记录作库存预检，
        # 不跨仓相加库存；最终整单履约仍由事务内自动分仓决定。
        live = [p for p in candidates if p.get('is_active', True)
                and p.get('warehouse_active', True)
                and not is_paused(max(0, int(stock_of(p['id']) or 0)),
                                  max(0, int(p.get('low_stock_threshold') or 0)))]
        return min(live or candidates,
                   key=lambda p: (-max(0, int(stock_of(p['id']) or 0)),
                                  int(p.get('id') or 0)), default=None)

    def available(product, needed):
        if not product.get('is_active',True) or not product.get('warehouse_active',True):
            return False
        return _available(
            stock_of(product['id']), product.get('low_stock_threshold'),
            needed, is_paused,
        )

    exact_only_norm = {normalize(x) for x in directive.exact_only}
    if directive.on_out_of_stock == POLICY_REPEAT_SELECTED:
        # Reserve every preferred quantity first. Only spare stock of these
        # same selected identities can fill a shortage, within its category.
        chosen={}; wanted={}; missing={}; failures=[]
        for item in items:
            product=lookup(item.get('category'),item.get('flavor'))
            if product is None:
                failures.append('客户已选口味不在有效目录中');continue
            pid=product['id']; chosen.setdefault(pid,(product,item));wanted[pid]=wanted.get(pid,0)+int(item.get('quantity') or 0)
        reserved={pid:min(q,max(0,int(stock_of(pid) or 0))) if available(chosen[pid][0],1) else 0 for pid,q in wanted.items()}
        for pid,q in wanted.items():
            category=normalize(chosen[pid][0].get('parent_name'))
            missing[category]=missing.get(category,0)+q-reserved[pid]
        for category,need in missing.items():
            for pid,(product,item) in chosen.items():
                if normalize(product.get('parent_name'))!=category or not available(product,1):continue
                extra=min(need,max(0,int(stock_of(pid) or 0)-reserved[pid]))
                reserved[pid]+=extra;need-=extra
                if not need:break
            if need:failures.append('客户已选口味库存不足，仍缺 '+str(need)+' 支')
        if failures:return [],[],failures
        planned=[dict(item,quantity=reserved[pid],product_id=pid) for pid,(product,item) in chosen.items() if reserved[pid]]
        return planned,['仅从客户已选同品类口味重复补足缺货数量'],[]
    exact_only_norm.discard('')

    # 订单原本就点了的商品。备选替换若指向其中之一，要合并数量而不是新开一行，
    # 也不能丢掉——丢掉会少发货。
    taken = set()
    for item in items:
        product = lookup(item.get('category'), item.get('flavor'))
        if product:
            taken.add(product.get('id'))

    final: List[Dict] = []
    actions: List[str] = []
    errors: List[str] = []
    dropped = 0
    merge_extra: Dict[int, List] = {}   # product_id -> [累计数量, 说明标签]

    for item in items:
        category = item.get('category')
        flavor = item.get('flavor')
        quantity = int(item.get('quantity') or 0)
        label = f'品类({category})口味({flavor})'
        product = lookup(category, flavor)
        if not product:
            errors.append(f'{label} 不在目录中')
            continue

        # 只发X：exact_only 来自客户原话，item 里既有目录规范名也有原话，两边都比
        # ——只比规范名会在「只发薄荷」而目录名是「灵魂薄荷」时把整单误清空。
        if exact_only_norm:
            candidates = {normalize(flavor), normalize(item.get('raw_flavor'))}
            candidates.discard('')
            if not (candidates & exact_only_norm):
                dropped += 1
                actions.append(f'{label} 未在「只发」清单内，已移除')
                continue

        if available(product, quantity):
            final.append({**item, 'product_id': product['id']})
            continue

        # ---- 以下都是缺货分支 ----
        policy = directive.on_out_of_stock
        if policy == POLICY_NONE:
            errors.append(f'{label} 库存不足，且客户未说明缺货如何处理')
            continue
        if policy == POLICY_EXACT:
            errors.append(f'{label} 库存不足；客户要求精准发货，未替换、未删减，本次未录单')
            continue
        if policy == POLICY_CANCEL_LINE:
            dropped += 1
            reason = '精准发货，不替换' if policy == POLICY_EXACT else '客户要求缺货退单'
            actions.append(f'{label} 库存不足，{reason}，已移除')
            continue

        substitute = None
        merged_into_existing = False
        if policy == POLICY_ALTERNATE:
            wanted = directive.alternates.get(str(flavor or '').strip())
            options=wanted if isinstance(wanted,list) else [wanted]
            # Keep every available unit of the customer's preferred flavor;
            # authorization to use a backup applies only to the shortage.
            # Build the replacement plan off to the side so an insufficient
            # backup never leaks a partial order into ``final``.
            keep=min(quantity,max(0,int(stock_of(product['id']) or 0))) if available(product,1) else 0
            remaining=quantity-keep
            partial=[]
            if keep and remaining:
                seen=set()
                for option in options:
                    candidate=lookup(category,option) if option else None
                    if not candidate or candidate.get('id') in taken or candidate.get('id') in seen:
                        continue
                    seen.add(candidate.get('id'))
                    if not available(candidate,1):continue
                    count=min(remaining,max(0,int(stock_of(candidate['id']) or 0)))
                    if count:
                        partial.append({**item,'quantity':count,'category':candidate.get('parent_name'),
                            'flavor':candidate.get('name'),'product_id':candidate['id'],
                            'substituted_from':flavor})
                        remaining-=count
                    if not remaining:break
                if remaining:
                    errors.append(f'{label} 主品仅有 {keep}，客户指定备选仍不足 {remaining} 支')
                    continue
                final.append({**item,'quantity':keep,'product_id':product['id']})
                final.extend(partial)
                actions.append(f'{label} 保留主品 {keep} 支，缺口 {quantity-keep} 支按客户备选替换')
                continue
            for option in options:
                candidate = lookup(category, option) if option else None
                if not candidate:continue
                if candidate.get('id') in taken:
                    original_quantity = sum(
                        int(row.get('quantity') or 0) for row in items
                        if lookup(row.get('category'), row.get('flavor')) is candidate)
                    prior_extra = merge_extra.get(candidate['id'], [0, []])[0]
                    if not available(candidate, original_quantity + prior_extra + quantity):
                        continue
                    # 备选正好是订单里已有的口味（可能还没轮到处理），先记账，
                    # 循环结束后统一合并，避免出现两行同一个商品。
                    entry = merge_extra.setdefault(candidate['id'], [0, []])
                    entry[0] += quantity
                    entry[1].append(label)
                    merged_into_existing=True
                    break
                if available(candidate, quantity):
                    substitute = candidate
                    break
        elif policy == POLICY_RANDOM:
            # Customer backup-random consent applies only to the shortage,
            # never to the portion of the requested flavor already available.
            keep=min(quantity,max(0,int(stock_of(product['id']) or 0))) if available(product,1) else 0
            remaining=quantity-keep
            pool = []
            for cand in products:
                if normalize(cand.get('parent_name')) != normalize(category):
                    continue
                if cand.get('id') in taken:
                    continue
                if not cand.get('is_archived') and available(cand, 1):
                    pool.append((stock_of(cand['id']), str(cand.get('name') or ''), cand))
            pool.sort(key=lambda x: (-x[0], x[1]))
            replacements=[];seen_flavors=set()
            for stock,name,cand in pool:
                identity=normalize(name)
                if identity in seen_flavors:continue
                seen_flavors.add(identity)
                count=min(remaining,max(0,int(stock)))
                if count:
                    replacements.append({**item,'quantity':count,'category':cand.get('parent_name'),
                        'flavor':name,'product_id':cand['id'],'substituted_from':flavor})
                    remaining-=count
                if not remaining:break
            if remaining:
                errors.append(f'{label} 缺货随机可用库存不足，仍缺 {remaining} 支')
                continue
            if keep:final.append({**item,'quantity':keep,'product_id':product['id']})
            final.extend(replacements)
            taken.update(row['product_id'] for row in replacements)
            actions.append(f'{label} 保留 {keep} 支，仅按客户授权随机补足 {quantity-keep} 支')
            continue

        if merged_into_existing:
            continue
        if substitute:
            taken.add(substitute.get('id'))
            final.append({
                **item,
                'category': substitute.get('parent_name'),
                'flavor': substitute.get('name'),
                'product_id': substitute.get('id'),
                'substituted_from': flavor,
            })
            how = '按客户指定备选' if policy == POLICY_ALTERNATE else '按缺货随机'
            actions.append(f'{label} 库存不足，{how}替换为 口味({substitute.get("name")})')
            continue

        how = '客户指定备选也无货' if policy == POLICY_ALTERNATE else '同品类均无货'
        errors.append(f'{label} 库存不足，{how}；未删减原订单，请调整口味后重新提交')

    # 合并那些「备选指向订单里已有口味」的数量
    for product_id, (extra, labels) in merge_extra.items():
        row = next((r for r in final if r.get('product_id') == product_id), None)
        product = by_id.get(product_id)
        joined = '、'.join(labels)
        if row is None or product is None:
            dropped += len(labels)
            actions.append(f'{joined} 库存不足，备选也无货，已移除')
            continue
        need = int(row.get('quantity') or 0) + extra
        if available(product, need):
            row['quantity'] = need
            actions.append(
                f'{joined} 库存不足，按客户指定备选合并进 '
                f'口味({product.get("name")})，数量累加为 {need}'
            )
        else:
            dropped += len(labels)
            actions.append(
                f'{joined} 库存不足，备选 口味({product.get("name")}) '
                f'不够合并（需 {need}），已移除'
            )

    if directive.selection_scope == 'selected_mutual':
        wanted=sum(int(item.get('quantity') or 0) for item in items)
        if errors or dropped or sum(int(row.get('quantity') or 0) for row in final)!=wanted:
            return [],[],['客户已选互备口味库存不足，无法保持原订单数量']
    if not final:
        if dropped:
            errors.append('按客户的缺货要求处理后，整单已无可发商品')
        elif not errors:
            errors.append('订单没有可发商品')
    return final, actions, errors
