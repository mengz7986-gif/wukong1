"""DeepSeek order extraction client.

The model only extracts fields. Product identity, price, stock and order
creation remain controlled by the local catalog and API.
"""

import json
import logging
import re
import base64
import hashlib
from request_memo import request_memoized
from typing import Any, Dict, List, Optional

import requests

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_VISION_MODEL,
    DEEPSEEK_TIMEOUT,
)
from deepseek_prompt import build_messages
from ai_settings import get_settings
from ai_transport import completion, AIRequestError
from ai_diagnostics import event as diagnostic_event

logger = logging.getLogger(__name__)


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or '').strip()
    if text.casefold() in {'null', 'none', 'n/a', 'unknown', '不确定', '未知', '无法确认'}:
        return None
    return text or None


def _json_content(content: str) -> Dict[str, Any]:
    text = str(content or '').strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('DeepSeek 返回的 JSON 不是对象')
    return value


def _normalise_result(value: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    normalization_codes = []
    for raw in value.get('items') or []:
        if not isinstance(raw, dict):
            normalization_codes.append('INVALID_RESPONSE')
            continue
        raw_flavor = _optional_text(raw.get('raw_flavor'))
        raw_category = _optional_text(raw.get('raw_category'))
        # flavor 可能为空（模型判断目录里没这个口味）——此时用客户原话兜底，
        # 由 deepseek_resolver 去做映射，绝不在这里丢掉商品。
        flavor = _optional_text(
            raw.get('flavor') or raw.get('product') or raw.get('name')
        ) or raw_flavor or ''
        category = _optional_text(raw.get('category') or raw.get('model')) or raw_category
        source_text = str(raw.get('source_text') or raw.get('evidence') or '').strip()
        try:
            quantity = int(raw.get('quantity'))
        except (TypeError, ValueError):
            normalization_codes.append('QUANTITY_INVALID')
            continue
        if flavor and quantity > 0:
            items.append({
                'category': category,
                'flavor': flavor,
                'quantity': quantity,
                'source_text': source_text,
                'raw_category': raw_category,
                'raw_flavor': raw_flavor,
                'confidence': raw.get('confidence', value.get('confidence', 0)),
                'flavor_candidates': raw.get('flavor_candidates') if isinstance(raw.get('flavor_candidates'),list) else [],
            })
        else:
            normalization_codes.append('QUANTITY_INVALID' if quantity <= 0 else 'INVALID_RESPONSE')

    alternates: List[Dict[str, Any]] = []
    for raw in value.get('alternates') or []:
        if not isinstance(raw, dict):
            continue
        alternates.append({
            'for': _optional_text(raw.get('for')),
            'flavor': _optional_text(raw.get('flavor')),
            'source_text': str(raw.get('source_text') or '').strip(),
            'raw_flavor': _optional_text(raw.get('raw_flavor')),
            'confidence': raw.get('confidence', value.get('confidence', 0)),
            'flavor_candidates': raw.get('flavor_candidates') if isinstance(raw.get('flavor_candidates'),list) else [],
        })

    def _optional_int(raw: Any) -> Optional[int]:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    try:
        confidence = float(value.get('confidence', 0))
    except (TypeError, ValueError):
        confidence = 0.0

    raw_notes = value.get('notes') or []
    raw_missing = value.get('missing') or []
    if isinstance(raw_notes, str):
        raw_notes = [raw_notes]
    if isinstance(raw_missing, str):
        raw_missing = [raw_missing]
    notes = [str(x).strip() for x in raw_notes if str(x).strip()]
    missing = [str(x).strip() for x in raw_missing if str(x).strip()]
    review_value = value.get('needs_manual_review')
    needs_manual_review = (
        review_value is True
        or str(review_value or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    raw_fulfillment = value.get('fulfillment')
    if not isinstance(raw_fulfillment, dict):
        raw_fulfillment = {}
    fulfillment = {
        'on_out_of_stock': _optional_text(raw_fulfillment.get('on_out_of_stock')),
        'exact_only': [
            str(x).strip() for x in (raw_fulfillment.get('exact_only') or [])
            if str(x).strip()
        ],
        'is_aftersale': bool(raw_fulfillment.get('is_aftersale')),
    }

    if normalization_codes:
        diagnostic_event('NORMALIZATION_RESULT', 'SUCCESS', normalization_codes)
    return {
        'address': _optional_text(value.get('address')),
        'recipient': _optional_text(value.get('recipient')),
        'phone': _optional_text(value.get('phone')),
        'warehouse': _optional_text(value.get('warehouse')),
        'items': items,
        'alternates': alternates,
        'fulfillment': fulfillment,
        'declared_total': _optional_int(value.get('declared_total')),
        'computed_total': _optional_int(value.get('computed_total')),
        'confidence': max(0.0, min(1.0, confidence)),
        'missing': missing,
        'notes': notes,
        'needs_manual_review': needs_manual_review,
    }


def parse_order(text: str, catalog: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    settings = get_settings({'api_key': DEEPSEEK_API_KEY, 'base_url': DEEPSEEK_BASE_URL, 'model': DEEPSEEK_MODEL, 'timeout': DEEPSEEK_TIMEOUT})
    if not settings['enabled'] or not settings['order_enabled']:
        raise AIRequestError('AI_DISABLED')
    from ai_prompt_scope import scope_catalog
    catalog = scope_catalog(text,catalog or [])
    catalog = [{'category': str(row.get('category') or ''), 'flavor': str(row.get('flavor') or '')}
               for row in (catalog or [])[:1200] if isinstance(row, dict) and row.get('flavor')]
    return completion(settings, build_messages(text, catalog), 'ORDER_PARSE',
                      validator=lambda content: _normalise_result(_json_content(content)))


def _contact_ai_memo_key(text, products=()):
    # Request-local only. Config/catalog changes invalidate cached candidates;
    # neither customer text nor credentials are written to disk or telemetry.
    material=json.dumps([text,products,get_settings()],ensure_ascii=False,sort_keys=True,default=str)
    return hashlib.sha256(material.encode()).hexdigest()


@request_memoized(_contact_ai_memo_key)
def parse_contact_ai(text, products=()):
    settings = get_settings()
    if not settings['enabled'] or not settings['contact_enabled']:
        raise AIRequestError('AI_DISABLED')
    from order_structure import structure_order_text
    structured=structure_order_text(text, products).text
    from contact_fields import contact_catalog_names
    merchandise_names = contact_catalog_names(products)
    contact_lines=[]
    merchandise_lines=[]
    for line in structured.splitlines():
        goods=bool(re.search(r'\d+\s*(?:支|根|件|个)|(?:×|x)\s*\d+',line))
        # A live category heading is merchandise evidence even when its
        # quantity appears on the following flavor line.  Without the live
        # catalog, the contact model can mistake this bare heading for a name.
        catalog_heading = line.strip().casefold() in merchandise_names
        contact=bool(re.search(r'1[3-9]\d{9}|收件|姓名|地址|省|市|区|县|街道|路|楼|栋|室',line))
        if goods or catalog_heading:
            merchandise_lines.append(line)
        if (not goods and not catalog_heading) or contact:contact_lines.append(line)
    return completion(settings, [
        {'role': 'system', 'content': '从客户完整原文寻找收件信息及纠正字段。返回JSON recipient, phone, address, confidence, name_absent。姓名可为英文简称或单字母，可能在手机号前后。地址可跨行，保留原文已有行政区、街道及详细门牌，不得编造。只有全文确实没有收件人姓名时 name_absent=true、recipient=null，否则false。未提供字段为null。数量关联商品行中的品牌/品类/口味不是收件人；不能为凑齐字段把商品名称提取为姓名。订单编号、仓库标签和客户附带金额不是姓名或地址。用户内容包含完整原文和保守收件区域提示，提示仅辅助定位，必须回到完整原文核对证据。'},
        {'role': 'user', 'content': json.dumps({'完整原文':str(text),'收件区域提示':'\n'.join(contact_lines), '禁止作为姓名的商品区域':merchandise_lines, '核对要求':'confidence必须为0到1的JSON数字，不能使用high等文字。仅手机号、地址而没有独立姓名时，必须返回recipient=null,name_absent=true；不能将禁止区域中的名称当姓名。'},ensure_ascii=False)},
    ], 'CONTACT_PARSE', validator=lambda content: {
        key: _json_content(content).get(key) for key in ('recipient', 'phone', 'address', 'confidence', 'name_absent')
    })


def parse_order_image(
    image_bytes: bytes,
    catalog: Optional[List[Dict[str, str]]] = None,
    caption: str = '',
    mime_type: str = 'image/jpeg',
) -> Dict[str, Any]:
    """Parse an order image through a configured OpenAI-compatible vision API.

    This interface is intentionally separate from the default inventory OCR
    handler. It lets a future Telegram photo flow reuse the same strict
    catalog validation without changing the text order path.
    """
    settings = get_settings({'api_key': DEEPSEEK_API_KEY, 'base_url': DEEPSEEK_BASE_URL, 'model': DEEPSEEK_VISION_MODEL, 'timeout': DEEPSEEK_TIMEOUT})
    if not settings['enabled'] or not settings['order_enabled']:
        raise AIRequestError('AI_DISABLED')
    if not image_bytes:
        raise ValueError('订单图片为空')

    catalog = catalog or []
    catalog = [
        {
            'category': str(row.get('category') or '').strip(),
            'flavor': str(row.get('flavor') or '').strip(),
        }
        for row in catalog[:1200]
        if isinstance(row, dict) and str(row.get('flavor') or '').strip()
    ]
    text_prompt = build_messages(caption or '请识别图片中的订单信息。', catalog)[-1]
    prompt_text = text_prompt.get('content', '') if isinstance(text_prompt, dict) else str(text_prompt)
    if not isinstance(prompt_text, str):
        prompt_text = str(prompt_text)
    data_url = f'data:{mime_type};base64,{base64.b64encode(image_bytes).decode("ascii")}'
    payload = {
        'model': DEEPSEEK_VISION_MODEL,
        'temperature': 0,
        'response_format': {'type': 'json_object'},
        'messages': [
            *build_messages('', catalog)[:-1],
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt_text},
                    {'type': 'image_url', 'image_url': {'url': data_url}},
                ],
            },
        ],
    }
    return completion(settings, payload['messages'], 'ORDER_IMAGE_PARSE',
                      validator=lambda content: _normalise_result(_json_content(content)))


def to_order_info(result: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    """Convert model output to the existing OrderParser contract."""
    address = result.get('address')
    if address:
        try:
            from address_utils import normalize_cn_address

            normalized = normalize_cn_address(address)
            address = normalized.get('normalized_address') or address
        except Exception:
            pass
    items = []
    for item in result.get('items') or []:
        category = str(item.get('category') or '').strip()
        flavor = str(item.get('flavor') or '').strip()
        quantity = int(item.get('quantity') or 0)
        if not flavor or quantity <= 0:
            continue
        reference = f'品类({category})口味({flavor})' if category else flavor
        items.append((reference, quantity))
    try:
        provider = str(get_settings().get('provider') or 'DeepSeek')
    except Exception:
        provider = 'DeepSeek'
    parse_source = provider.casefold()
    return {
        'items': items,
        'address': address,
        'recipient': result.get('recipient'),
        'phone': result.get('phone'),
        'raw_text': raw_text,
        'parse_source': parse_source,
        'ai_confidence': result.get('confidence', 0.0),
        'ai_missing': result.get('missing') or [],
        'ai_notes': result.get('notes') or [],
        'ai_needs_manual_review': bool(result.get('needs_manual_review')),
        'ai_warehouse': result.get('warehouse'),
        'ai_alternates': result.get('alternates') or [],
        'ai_fulfillment': result.get('fulfillment') or {},
        'ai_items': [dict(x) for x in (result.get('items') or [])],
        'ai_declared_total': result.get('declared_total'),
        'ai_computed_total': result.get('computed_total'),
    }
