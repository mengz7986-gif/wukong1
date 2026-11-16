"""把客户原话确定性地映射到后台目录的品类 / 口味。

设计原则：**模型只负责抄客户原话，映射由这里做。**

理由：让模型做模糊匹配不可审计（同一条消息两次可能给不同答案），而且模型
会为了"匹配上"硬凑，那就是幻觉。这里全是确定性规则，可单测、可回归、错了
能精确定位到哪条规则。

映射失败时返回 None 加候选列表，**绝不猜**。要么补 EXPLICIT_FLAVOR 表，
要么走人工/追问客户。
"""

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from deepseek_prompt import MODEL_ALIASES

# --------------------------------------------------------------------------
# 口味同义词：客户写法 -> 目录名。水果类保持双向（组合口味还原要反向查）。
# 只放「确定是同一个东西」的，不放「差不多」的。
#
# 下面这批由 learn_catalog_aliases.py 让 DeepSeek 通读 402 条目录产出，
# 只采纳「跨 ≥2 个品类被一致建议」且人工复核过的（跨品类一致 = 真同义词，
# 只在单一品类出现 = 模型为了凑上目录硬编的）。
#
# 已明确拒绝、不要再加进来的（模型建议过但是错的）：
#   绿茶→龙井      绿茶不等于龙井，且悦刻板砖有真的「青梅绿茶」
#   柠檬→青柠      黄柠檬和青柠是两种货
#   椰奶→椰子      椰奶是椰浆味，不是椰子味
#   甜瓜↔哈密瓜    模型给了双向映射，自相矛盾，两边都不能信
#   提子→青提      提子在国内通常指葡萄
#   蓝莓→蓝拉滋    板砖没有蓝莓，模型硬凑
#   芒果西柚→杨枝甘露  同上，硬凑
# --------------------------------------------------------------------------
FLAVOR_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    '蜜桃乌龙': ('桃子乌龙',),
}

# --------------------------------------------------------------------------
# 显式口味别名：品类内自动规则解决不了、必须人工拍板一次的。
# 键是 (品类, 客户写法)，值是目录口味名。
# --------------------------------------------------------------------------
EXPLICIT_FLAVOR: Dict[Tuple[str, str], str] = {}

# 型号词上的装饰后缀/前缀，匹配不到时逐个剥掉再试
DECORATIONS = (
    '一次性', '冰砖', '冰盒', '冰弹', '双响炮', '透明', '新版', '旧版',
    '一代', '二代', '1代', '2代', '产品', '下单产品', '牌',
)

# 品类名里的同音/形近错字。客户把「崧」写成松/菘/嵩是高频错误。
# 只用于品类，不用于口味。
CATEGORY_CHAR_FIX = str.maketrans({})

# --------------------------------------------------------------------------
# 代际（新代 / 旧代）标记
#
# 血案：客户写「新款雪茄板砖」时，模糊匹配在文本里只能找到连续的「板砖」，找不到
# 连续的「新板砖」，于是**静默落到旧代品类**上——发错货且完全不报错。装饰词正则
# 把「2W / 20000 / 新版」删掉同理，而这些恰恰是区分两代的唯一依据。
#
# 因此两条铁律：
#   1) 代际一律在**客户原始写法**上判定，不能在剥完装饰词的残句上判定。
#   2) 模糊匹配命中后必须复核代际；跨代时改判到同系列的另一代，改判不了就拒绝。
#
# 「目录直接命中」和「别名表命中」是权威结论，不走这套复核。
# --------------------------------------------------------------------------

# 明确无歧义的代际词
STRONG_NEW_MARKERS = ('新款', '新版', '新式', '二代', '2代', '第二代')
STRONG_OLD_MARKERS = ('一代', '1代', '第一代', '旧款', '旧版', '老款', '老版')

# 容量数字 -> 代际。只给「确实知道两代容量不同」的系列配，不做「2W 一律是新代」
# 这种全局假设。键是该系列的基准（旧代）品类名。
CAPACITY_GENERATION: Dict[str, Dict[str, str]] = {
    '板砖': {
        '20000': 'new', '2w': 'new', '2万': 'new',
        '10000': 'old', '1w': 'old', '1万': 'old',
    },
}

# 目录里「新代」品类名相对基准名的构词方式
NEW_GEN_PREFIXES = ('新款', '新版', '新')
NEW_GEN_SUFFIXES = ('二代', '2代')


def _category_generation(name: Optional[str]) -> str:
    """目录品类名自身是新代还是基准代。返回 'new' / 'base'。"""
    token = _norm(name)
    if not token:
        return 'base'
    if any(_norm(m) in token for m in STRONG_NEW_MARKERS):
        return 'new'
    for prefix in NEW_GEN_PREFIXES:
        pt = _norm(prefix)
        if token.startswith(pt) and len(token) > len(pt):
            return 'new'
    return 'base'


def _strip_new_gen(name: Optional[str]) -> str:
    """新代品类名还原成基准名：新板砖→板砖、板砖二代→板砖。"""
    token = _norm(name)
    for prefix in sorted(NEW_GEN_PREFIXES, key=len, reverse=True):
        pt = _norm(prefix)
        if pt and token.startswith(pt) and len(token) > len(pt):
            return token[len(pt):]
    for suffix in sorted(NEW_GEN_SUFFIXES, key=len, reverse=True):
        st = _norm(suffix)
        if st and token.endswith(st) and len(token) > len(st):
            return token[:-len(st)]
    return token


def _raw_generation(
    raw_text: str,
    live_categories: Sequence[str],
) -> Tuple[Optional[str], str]:
    """从客户原始写法判定代际意图。

    返回 ('new' | 'old' | 'ambiguous_new' | 'conflict' | None, 说明)。
    """
    token = _norm(raw_text)
    if not token:
        return None, ''

    # 最强证据：目录里的新代品类名原样出现在客户写法里（新鸭嘴兽 里含「新鸭嘴」）
    for category in live_categories:
        ct = _norm(category)
        if ct and ct in token and _category_generation(category) == 'new':
            return 'new', f'原话含新代品类名({category})'

    has_new = any(_norm(m) in token for m in STRONG_NEW_MARKERS)
    has_old = any(_norm(m) in token for m in STRONG_OLD_MARKERS)

    for family, table in CAPACITY_GENERATION.items():
        if _norm(family) not in token:
            continue
        for value, generation in table.items():
            if _norm(value) in token:
                if generation == 'new':
                    has_new = True
                else:
                    has_old = True

    if has_new and has_old:
        return 'conflict', f'同时写了新代和旧代标记({raw_text})'
    if has_new:
        return 'new', '原话有新代标记'
    if has_old:
        return 'old', '原话有旧代标记'
    # 光秃秃一个「新」字：可能是「新款」，也可能是「新到货」，不敢直接当代际标记
    if token.startswith('新'):
        return 'ambiguous_new', '原话以「新」开头但没写清代际'
    return None, ''


def _sibling_generation(
    candidate: str,
    live_categories: Sequence[str],
    want_new: bool,
) -> Optional[str]:
    """在同一系列里找另一代的品类名。找不到返回 None。"""
    base = _strip_new_gen(candidate)
    for other in live_categories:
        if not other or _norm(other) == _norm(candidate):
            continue
        if _strip_new_gen(other) != base:
            continue
        if (_category_generation(other) == 'new') == want_new:
            return other
    return None


def _check_generation(
    candidate: str,
    want: Optional[str],
    want_reason: str,
    live_categories: Sequence[str],
    reason: str,
) -> Tuple[Optional[str], str]:
    """模糊命中后的代际复核。跨代就改判，改判不了且有歧义就拒绝。"""
    if want is None:
        return candidate, reason
    if want == 'conflict':
        return None, f'代际写法矛盾：{want_reason}'

    got = _category_generation(candidate)
    if want == 'new':
        if got == 'new':
            return candidate, f'{reason}|代际一致'
        sibling = _sibling_generation(candidate, live_categories, want_new=True)
        if sibling:
            return sibling, f'{reason}|代际修正({candidate}→{sibling}:{want_reason})'
        return candidate, f'{reason}|该系列只有一代'
    if want == 'old':
        if got != 'new':
            return candidate, f'{reason}|代际一致'
        sibling = _sibling_generation(candidate, live_categories, want_new=False)
        if sibling:
            return sibling, f'{reason}|代际修正({candidate}→{sibling}:{want_reason})'
        return candidate, f'{reason}|该系列只有一代'
    if want == 'ambiguous_new':
        if got == 'new':
            return candidate, f'{reason}|代际一致'
        sibling = _sibling_generation(candidate, live_categories, want_new=True)
        if sibling:
            # 两代都在售，客户只写了个「新」：宁可问一句，也不敢默认发旧代
            return None, (
                f'代际不明确：写了「新」，但没写清是「{candidate}」还是「{sibling}」'
            )
        return candidate, f'{reason}|该系列只有一代'
    return candidate, reason

_SPLIT = re.compile(r'[+＋&和、,，/／\s]+')


def _norm(value: Optional[str]) -> str:
    """与 bot.py 的 _normalize_catalog_token 保持一致。"""
    return re.sub(r'[\s\-_—/（）()]+', '', str(value or '')).casefold()


def _variants(word: str) -> List[str]:
    """一个词自己 + 它的所有同义词。"""
    out = [word]
    from alias_policy import legacy_aliases_enabled
    for syn in (FLAVOR_SYNONYMS.get(word, ()) if legacy_aliases_enabled() else ()):
        if syn not in out:
            out.append(syn)
    return out


def _covered(part: str, raw: str) -> bool:
    """判断组合口味的一个成分是否被客户原话覆盖。"""
    for variant in _variants(part):
        if variant and variant in raw:
            return True
    # 词根匹配：「乌龙茶」对客户写的「青提乌龙」
    return len(part) >= 3 and part[:2] in raw


# --------------------------------------------------------------------------
# 品类
# --------------------------------------------------------------------------
def resolve_category(raw_category, live_categories, products=None):
    """Strict identity: live standard or active audited Alias; never a guess."""
    from rule_dictionary import runtime_store
    from brand_aliases import scope_matches
    normalize=lambda value:re.sub(r'\s+','',str(value or '')).casefold()
    token=normalize(raw_category)
    if not token:return None,'未写商品分类'
    exact={name for name in live_categories if normalize(name)==token}
    if len(exact)==1:return next(iter(exact)),'后台标准分类精确命中'
    if len(exact)>1:return None,'后台分类名称不唯一'
    matches={row.get('standard') for row in runtime_store().get('aliases',[])
             if row.get('type')=='MODEL_ALIASES' and row.get('status')=='active'
             and ((products is not None and any(scope_matches(row,p) and p.get('parent_name')==row.get('standard') for p in products))
                  or (products is None and (row.get('scope')=='GLOBAL' or not row.get('category_id'))))
             and row.get('standard') in live_categories and normalize(row.get('alias'))==token}
    if len(matches)==1:return next(iter(matches)),'已审核分类叫法精确命中'
    if len(matches)>1:raise ValueError('全后台存在冲突品牌Alias，需管理员处理')
    return None,'商品分类未命中后台标准或已审核叫法；不猜测分类'


def enforce_generation(
    candidate: Optional[str],
    raw_text: Optional[str],
    live_categories: Sequence[str],
    allow_reject: bool = False,
) -> Tuple[Optional[str], str]:
    """给「已经用别的规则匹配到的品类」补一次代际复核，供 bot.py 手工下单路径复用。

    手工路径原来用前缀匹配（`_model_matches`），「板砖2W」会命中旧代「板砖」，
    和 AI 路径给出相反的答案。这里让两条路径共用同一套代际判据。

    allow_reject=False（默认）时**只做修正不做拒绝**：要么返回同系列正确那一代，
    要么原样返回。用在已经能正常下单的路径上，避免把「本来能下的单」变成拒绝。
    """
    if not candidate:
        return candidate, '无候选'
    want, want_reason = _raw_generation(str(raw_text or ''), live_categories)
    if not allow_reject and want in ('ambiguous_new', 'conflict'):
        return candidate, '代际不明确，保留原候选'
    fixed, reason = _check_generation(
        candidate, want, want_reason, live_categories, '外部匹配',
    )
    if fixed is None and not allow_reject:
        return candidate, reason
    return fixed, reason


# --------------------------------------------------------------------------
# 口味
# --------------------------------------------------------------------------
def resolve_flavor(
    category: Optional[str],
    raw_flavor: Optional[str],
    flavors: Iterable[str],
    products: Optional[Sequence[Dict]] = None,
) -> Tuple[Optional[str], str]:
    """客户写的口味 -> 该品类下的目录口味名。"""
    raw = str(raw_flavor or '').strip()
    pool = [f for f in flavors if f]
    if not raw:
        return None, '未写口味'
    if not pool:
        return None, f'品类({category})下无商品'

    from rule_dictionary import manual_flavor
    try:
        manual = manual_flavor(str(category or ''), raw, pool, products=products)
    except ValueError as error:
        return None, str(error)
    if manual:
        return manual, '人工Alias精确命中'
    from alias_policy import legacy_aliases_enabled
    explicit = EXPLICIT_FLAVOR.get((str(category or ''), raw)) if legacy_aliases_enabled() else None
    if explicit and explicit in pool:
        return explicit, '显式别名表'

    by_norm = {_norm(f): f for f in pool}
    token = _norm(raw)

    if token in by_norm:
        return by_norm[token], '精确命中'

    # 同义词必须排在子串启发式前面：同义词是人工复核过的确定知识，子串只是
    # 启发式。否则「黑薄荷」会被「长→短」抢先匹配成「薄荷」而不是「黑冰薄荷」，
    # 「葡萄柚」会被匹配成「葡萄」而不是「西柚」。
    for variant in _variants(raw)[1:]:
        vt = _norm(variant)
        if vt in by_norm:
            return by_norm[vt], f'同义词({raw}→{variant})'

    # 客户写得长，目录名更短：白桃乌龙 -> 白桃、冰薄荷 -> 薄荷
    # 必须排在「短→长」前面。崧弹同时有「薄荷」和「黑冰薄荷」，客户写「冰薄荷」
    # 时若先跑「短→长」会错配成「黑冰薄荷」——客户加修饰词比省略词常见得多。
    shorter = [f for f in pool if _norm(f) and _norm(f) in token]
    if len(shorter) == 1:
        return shorter[0], '唯一子串(长→短)'

    # 客户写得短，目录名更长：百香果 -> 西柚百香果
    longer = [f for f in pool if token and token in _norm(f)]
    if len(longer) == 1:
        return longer[0], '唯一子串(短→长)'

    # 同义词 + 子串组合：同义词本身不在目录里，但是某个目录名的一部分
    for variant in _variants(raw)[1:]:
        vt = _norm(variant)
        vl = [f for f in pool if vt and vt in _norm(f)]
        if len(vl) == 1:
            return vl[0], f'同义词+子串({raw}→{variant})'

    # 组合口味：目录写「A+B」，客户连写成一个词
    combos = []
    for flavor in pool:
        parts = [p for p in _SPLIT.split(flavor) if p]
        if len(parts) < 2:
            continue
        if all(_covered(p, raw) for p in parts):
            combos.append(flavor)
    if len(combos) == 1:
        return combos[0], '组合口味还原'
    if len(combos) > 1:
        return None, '组合口味多候选:' + '/'.join(combos)

    if len(longer) > 1:
        return None, '多候选:' + '/'.join(longer)
    if len(shorter) > 1:
        return None, '多候选:' + '/'.join(shorter)
    return None, f'品类({category})下无此口味({raw})'


# --------------------------------------------------------------------------
# 整条商品项
# --------------------------------------------------------------------------
def resolve_identity_flavor(category, raw_flavor, flavors, products=None):
    """Authorize identity by a live standard or effective Alias, never a guess.

    Heuristic ``resolve_flavor`` remains available for candidate discovery;
    its substring/synonym suggestions cannot authorize a primary order item.
    """
    raw=str(raw_flavor or '').strip()
    pool=list(dict.fromkeys(f for f in flavors if f))
    if not raw:return None,'未写口味'
    from rule_dictionary import manual_flavor
    try:
        approved=manual_flavor(str(category or ''),raw,pool,products=products)
    except ValueError as error:
        return None,str(error)
    if approved:return approved,'正式目录或人工Alias精确命中'
    exact=[name for name in pool if _norm(name)==_norm(raw)]
    if len(exact)==1:return exact[0],'正式目录格式归一化命中'
    if len(exact)>1:return None,'多候选:'+ '/'.join(exact)
    return None,f'品类({category})下无此口味({raw})：缺少正式目录或有效Alias证据'


def resolve_item(item: Dict, products: Sequence[Dict], *, customer_confirmed=False) -> Dict:
    """把一个 AI 商品项解析成目录里的规范 (品类, 口味)。

    返回新 dict，附带 resolved_category / resolved_flavor / resolve_reason。
    解析不出来时两者为 None，reason 说明原因。
    """
    live_categories = []
    seen = set()
    products=[p for p in products if p.get('is_active',True) and not p.get('is_archived')]
    for product in products:
        name = str(product.get('parent_name') or '').strip()
        if name and name not in seen:
            seen.add(name)
            live_categories.append(name)

    raw_category = item.get('raw_category') or item.get('category')
    raw_flavor = item.get('raw_flavor') or item.get('flavor')

    category, category_reason = resolve_category(raw_category, live_categories, products)
    # An unresolved explicit customer brand must remain unresolved. A model's
    # valid-looking catalog category is not an effective customer Alias.

    flavors = [
        str(p.get('name') or '').strip()
        for p in products
        if category and _norm(p.get('parent_name')) == _norm(category)
    ]
    flavor, flavor_reason = (None, category_reason)
    if category:
        scope_products = [p for p in products if _norm(p.get('parent_name')) == _norm(category)]
        flavor, flavor_reason = resolve_identity_flavor(category, raw_flavor, flavors, products=scope_products)
        # This flag is supplied only by the validated Pending callback's
        # confirmation set. Provider JSON cannot grant itself consent.
        if customer_confirmed and item.get('flavor') in flavors:
            flavor,flavor_reason=item['flavor'],'客户在当前商品Pending明确选择'

    result = dict(item)
    result.update({
        'resolved_category': category,
        'resolved_flavor': flavor,
        'resolve_reason': f'品类:{category_reason} | 口味:{flavor_reason}',
    })
    return result
