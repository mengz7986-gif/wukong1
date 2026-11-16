# -*- coding: utf-8 -*-
"""停仓规则（按仓库）。**两台机器人字节必须一致。**

为什么单独一个模块：停售/可售判断曾经散在三处实现，改一处漏一处就会一台停一台不停。
停仓同样会出现在 config（读）、bot（拦单）、api（写开关）三个地方，规则只放这里。

`settings.json` 的 `sales` 段结构：

    "sales": {
      "paused": false,          # 派生值：所有仓库都停时为 true。老代码只读这个也不会错
      "message": "给客户的提示语",
      "paused_at": "",
      "warehouses": {           # 真正的开关，键是仓库 id 的字符串形式
        "5": {"paused": true, "message": "", "paused_at": "2026-08-23 10:00:00"}
      }
    }

停仓只挡「新订单进入」，不影响已生成订单的发货、打单、售后。
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_MESSAGE = (
    '🛑 当前已暂停接单\n\n仓库正在盘点/补货，暂时无法下单。\n开仓后会第一时间通知，感谢理解 🙏'
)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def wid(warehouse_id: Any) -> str:
    """仓库 id 一律用字符串做键：JSON 的键只能是字符串，避免 5 和 '5' 两套。"""
    return str(warehouse_id if warehouse_id is not None else '').strip()


def normalize(section: Optional[Dict]) -> Dict:
    """把 sales 段规整成固定结构，缺字段补默认值。"""
    data = section if isinstance(section, dict) else {}
    warehouses: Dict[str, Dict] = {}
    raw = data.get('warehouses')
    if isinstance(raw, dict):
        for key, value in raw.items():
            key = wid(key)
            if not key:
                continue
            value = value if isinstance(value, dict) else {'paused': value}
            warehouses[key] = {
                'paused': _truthy(value.get('paused')),
                'message': str(value.get('message') or '').strip(),
                'paused_at': str(value.get('paused_at') or '').strip(),
            }
    return {
        'paused': _truthy(data.get('paused')),
        'message': str(data.get('message') or '').strip(),
        'paused_at': str(data.get('paused_at') or '').strip(),
        'warehouses': warehouses,
    }


def is_paused(section: Optional[Dict], warehouse_id: Any) -> bool:
    """某个仓库当前是否停仓。全局位为真时所有仓库都停（兼容旧配置和环境变量）。"""
    data = normalize(section)
    if data['paused']:
        return True
    entry = data['warehouses'].get(wid(warehouse_id))
    return bool(entry and entry['paused'])


def message_for(
    section: Optional[Dict],
    warehouse_id: Any = None,
    fallback: Optional[str] = None,
) -> str:
    """给客户看的停仓提示语：仓库自己的 > 全局的 > 调用方兜底 > 模块默认。"""
    data = normalize(section)
    entry = data['warehouses'].get(wid(warehouse_id)) or {}
    for text in (entry.get('message'), data['message'], fallback, DEFAULT_MESSAGE):
        text = str(text or '').strip()
        if text:
            return text
    return DEFAULT_MESSAGE


def paused_at_for(section: Optional[Dict], warehouse_id: Any = None) -> str:
    data = normalize(section)
    entry = data['warehouses'].get(wid(warehouse_id)) or {}
    return entry.get('paused_at') or data['paused_at'] or ''


def _ids(warehouses: Iterable) -> List[str]:
    out = []
    for warehouse in warehouses or []:
        if isinstance(warehouse, dict):
            key = wid(warehouse.get('id'))
        else:
            key = wid(warehouse)
        if key and key not in out:
            out.append(key)
    return out


def active_warehouses(section: Optional[Dict], warehouses: Sequence[Dict]) -> List[Dict]:
    """还在接单的仓库（既没被停仓、也没被后台停用）。"""
    return [
        warehouse for warehouse in warehouses or []
        if warehouse.get('is_active', True)
        and not is_paused(section, warehouse.get('id'))
    ]


def paused_warehouses(section: Optional[Dict], warehouses: Sequence[Dict]) -> List[Dict]:
    return [
        warehouse for warehouse in warehouses or []
        if warehouse.get('is_active', True)
        and is_paused(section, warehouse.get('id'))
    ]


def all_paused(section: Optional[Dict], warehouses: Sequence[Dict]) -> bool:
    """是否所有可用仓库都停了 —— 等价于原来的「一键停仓」。

    没有任何仓库时返回全局位：此时谈不上按仓库停，只能看全局。
    """
    usable = [w for w in warehouses or [] if w.get('is_active', True)]
    if not usable:
        return normalize(section)['paused']
    return all(is_paused(section, w.get('id')) for w in usable)


def materialize(section: Optional[Dict], warehouses: Sequence[Dict]) -> Dict:
    """把旧的全局 `paused` 摊到每个仓库上，然后清掉全局位。

    不做这一步的话：全局位还是 true，用户单独开某个仓会「点了没反应」。
    """
    data = normalize(section)
    if data['paused']:
        for key in _ids(warehouses):
            entry = data['warehouses'].setdefault(
                key, {'paused': False, 'message': '', 'paused_at': ''}
            )
            if not entry['paused']:
                entry['paused'] = True
                entry['paused_at'] = data['paused_at']
        data['paused'] = False
    return data


def apply_pause(
    section: Optional[Dict],
    warehouses: Sequence[Dict],
    paused: bool,
    warehouse_ids: Optional[Iterable] = None,
    message: Optional[str] = None,
    now: str = '',
) -> Dict:
    """写开关。`warehouse_ids` 为空表示全部仓库（保持旧的「一键停仓」语义）。

    返回新的 sales 段；`paused` 字段按「是否所有仓库都停」重新算出来，
    这样只读旧字段的代码也不会误判。
    """
    data = materialize(section, warehouses)
    known = _ids(warehouses)
    targets = [wid(x) for x in (warehouse_ids or [])] or list(known)
    targets = [t for t in targets if t]
    text = str(message or '').strip()
    if text:
        data['message'] = text
    for key in targets:
        entry = data['warehouses'].setdefault(
            key, {'paused': False, 'message': '', 'paused_at': ''}
        )
        entry['paused'] = bool(paused)
        entry['paused_at'] = now if paused else ''
    # 已经不存在的仓库不留垃圾
    if known:
        for key in list(data['warehouses']):
            if key not in known:
                data['warehouses'].pop(key, None)
    data['paused'] = all_paused(data, warehouses)
    data['paused_at'] = now if data['paused'] else ''
    return data


def describe(section: Optional[Dict], warehouses: Sequence[Dict]) -> List[Dict]:
    """给后台界面用的每仓一行。"""
    data = normalize(section)
    rows = []
    for warehouse in warehouses or []:
        key = wid(warehouse.get('id'))
        entry = data['warehouses'].get(key) or {}
        rows.append({
            'warehouse_id': warehouse.get('id'),
            'name': warehouse.get('name') or f'仓库{key}',
            'is_active': bool(warehouse.get('is_active', True)),
            'paused': is_paused(data, warehouse.get('id')),
            'paused_at': entry.get('paused_at') or (data['paused_at'] if data['paused'] else ''),
            'message': entry.get('message') or '',
        })
    return rows
