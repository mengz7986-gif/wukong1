# -*- coding: utf-8 -*-
"""风火递开放平台客户端（按官方订单对接文档）"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import requests

from address_utils import split_cn_address as normalize_split_cn_address


def _display_order_item_name(item: Any) -> str:
    """Keep category context in the name sent to the shipping provider."""
    product = getattr(item, 'product', None)
    product_name = str(getattr(product, 'name', None) or getattr(item, 'product_id', '') or '')
    parent = getattr(product, 'parent', None)
    parent_name = str(getattr(parent, 'name', None) or '').strip()
    if parent_name and parent_name != product_name:
        return f'{parent_name}/{product_name}'
    return product_name


def hmac_md5_hex(key: str, content: str) -> str:
    dig = hmac.new(key.encode('utf-8'), content.encode('utf-8'), hashlib.md5).digest()
    return dig.hex()


def build_sign(psecret: str, pid: str, time_ms: int, nonce_str: str, params_json: str) -> str:
    """
    官方签名：
    1) 对 pid/time/nonceStr/params 按参数名排序
    2) 参数名+参数值拼接，前后加 psecret
    3) HMAC_MD5(psecret, signString) 十六进制
    """
    pairs = {
        'nonceStr': str(nonce_str),
        'params': str(params_json),
        'pid': str(pid),
        'time': str(int(time_ms)),
    }
    items = [f'{k}{pairs[k]}' for k in sorted(pairs.keys())]
    sign_string = f"{psecret}{''.join(items)}{psecret}"
    return hmac_md5_hex(psecret, sign_string)


def split_cn_address(address: str) -> Dict[str, str]:
    """推送风火递前标准化中文地址。"""
    parsed = normalize_split_cn_address(address)
    return {
        'province': parsed.get('province') or '未知省',
        'city': parsed.get('city') or '未知市',
        'district': parsed.get('district') or '',
        'town': parsed.get('town') or '',
        'detail': parsed.get('detail') or address or '未知地址',
    }


def validate_recipient_address(address: str) -> Dict[str, str]:
    """Normalize a recipient address and require a resolvable area.

    For direct-admin cities, ``split_cn_address`` maps the verified town or
    street to ``district`` because Fenghuodi's ``receiverArea`` must not be
    empty even though no district/county exists in the administrative tree.
    """
    parsed = split_cn_address(address)
    if parsed.get('province') in {'', '未知省'} or parsed.get('city') in {'', '未知市'}:
        raise ValueError('收货地址缺少省或市，请补充完整行政区')
    if not parsed.get('district'):
        raise ValueError('收货地址缺少区/县，无法申请电子面单，请补充区县')
    return parsed


def re_search_addr(address: str):
    import re
    patterns = [
        r'^(?P<prov>.+?(?:省|自治区|特别行政区|北京市|天津市|上海市|重庆市))(?P<city>.+?(?:市|自治州|地区|盟))?(?P<dist>.+?(?:区|县|市|旗))?(?P<detail>.*)$',
        r'^(?P<prov>北京|天津|上海|重庆)(?P<city>市)?(?P<dist>.+?(?:区|县))?(?P<detail>.*)$',
    ]
    for p in patterns:
        m = re.match(p, address)
        if not m:
            continue
        prov = m.groupdict().get('prov') or ''
        city = m.groupdict().get('city') or ''
        dist = m.groupdict().get('dist') or ''
        detail = (m.groupdict().get('detail') or '').strip() or address
        if prov in ('北京', '天津', '上海', '重庆'):
            prov = prov + '市'
            city = city or '市辖区'
        if prov and not city:
            # 直辖市
            if prov.endswith(('北京市', '天津市', '上海市', '重庆市')):
                city = '市辖区'
        return prov, city, dist, detail
    return None


class FengHuoDiClient:
    def __init__(
        self,
        pid: str,
        psecret: str,
        user_id: str = '',
        base_url: str = 'https://openapi.fhd001.com',
        sender: Optional[Dict[str, str]] = None,
        timeout: int = 20,
    ):
        self.pid = str(pid or '').strip()
        self.psecret = str(psecret or '').strip()
        self.user_id = str(user_id or '').strip()
        self.base_url = (base_url or 'https://openapi.fhd001.com').rstrip('/')
        self.sender = sender or {}
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.pid and self.psecret)

    def _request(self, path: str, params_obj: Dict[str, Any]) -> Dict[str, Any]:
        if not self.configured:
            raise RuntimeError('未配置风火递 pid/psecret')
        params_json = json.dumps(params_obj, ensure_ascii=False, separators=(',', ':'))
        time_ms = int(time.time() * 1000)
        nonce = uuid.uuid4().hex
        sign = build_sign(self.psecret, self.pid, time_ms, nonce, params_json)
        form = {
            'pid': self.pid,
            'time': str(time_ms),
            'nonceStr': nonce,
            'params': params_json,
            'sign': sign,
        }
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        resp = requests.post(
            url,
            data=form,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=self.timeout,
        )
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f'风火递返回非JSON: HTTP {resp.status_code} {resp.text[:300]}')
        return {
            'http_status': resp.status_code,
            'request': {
                'url': url,
                'pid': self.pid,
                'time': time_ms,
                'nonceStr': nonce,
                'params': params_obj,
            },
            'response': data,
            'success': str(data.get('rcode')) in ('0', '0.0') and str(data.get('scode')) in ('0', '0.0'),
        }

    def get_logistics_companies(self) -> Dict[str, Any]:
        return self._request('/order/getLogisticsCompanyList.do', {})

    def get_merchants(self, page: int = 1, page_size: int = 40) -> Dict[str, Any]:
        return self._request('/order/v2/getMerchants.do', {'page': page, 'pageSize': page_size})

    def get_merchant(self, merchant_id: str) -> Dict[str, Any]:
        return self._request('/order/v2/getMerchant.do', {'merchantId': str(merchant_id)})

    def get_auth_users(self) -> Dict[str, Any]:
        # 文档中有授权用户相关接口；列表接口名以 getAuthUsers 兼容常见实现
        # 若失败，调用方仍可手工填写 userId
        for path in ('/order/getAuthUsers.do', '/order/getAuthUserList.do'):
            try:
                return self._request(path, {})
            except Exception:
                continue
        return self._request('/order/getAuthUsers.do', {})

    def merchant_auth_url(self, merchant_id: str, merchant_name: str, redirect_uri: str = '') -> str:
        q = {
            'pid': self.pid,
            'merchantId': quote(str(merchant_id), safe=''),
            'merchantName': quote(str(merchant_name), safe=''),
        }
        if redirect_uri:
            q['redirectUri'] = quote(str(redirect_uri), safe='')
        # 注意：文档要求 merchantId/Name 先 URLEncode 再放入 query
        return (
            f"{self.base_url}/page/merchantAuth.html"
            f"?pid={q['pid']}&merchantId={q['merchantId']}&merchantName={q['merchantName']}"
            + (f"&redirectUri={q['redirectUri']}" if redirect_uri else '')
        )

    def user_auth_url(self, user_id: str, user_name: str, redirect_uri: str = '') -> str:
        q = {
            'pid': self.pid,
            'userId': quote(str(user_id), safe=''),
            'userName': quote(str(user_name), safe=''),
        }
        if redirect_uri:
            q['redirectUri'] = quote(str(redirect_uri), safe='')
        return (
            f"{self.base_url}/page/auth.html"
            f"?pid={q['pid']}&userId={q['userId']}&userName={q['userName']}"
            + (f"&redirectUri={q['redirectUri']}" if redirect_uri else '')
        )

    def build_order_info_from_local(
        self,
        order,
        package_note: str = '',
        items: Optional[List[Any]] = None,
        order_id_suffix: str = '',
    ) -> Dict[str, Any]:
        sender_name = self.sender.get('name') or '发件人'
        sender_mobile = self.sender.get('mobile') or self.sender.get('phone') or ''
        sender_phone = self.sender.get('phone') or ''
        sender_addr = split_cn_address(self.sender.get('address') or '')

        recipient_name = getattr(order, 'recipient_name', None) or ''
        recipient_mobile = getattr(order, 'recipient_phone', None) or ''
        recipient_addr = validate_recipient_address(getattr(order, 'recipient_address', None) or '')

        items = []
        category = '日用'
        try:
            for it in (items if items is not None else (order.items or [])):
                pname = _display_order_item_name(it)
                items.append(f'{pname}x{it.quantity}')
                # 粗略品类
                if '烟' in pname:
                    category = '烟具'
        except Exception:
            pass
        note = package_note or (' '.join(items)[:120])

        base_order_id = str(getattr(order, 'order_number', None) or getattr(order, 'id', '') or '')
        order_id = f'{base_order_id}{order_id_suffix}'[:31]
        sender_obj = {
            'name': sender_name[:31],
            'address': {
                'province': (sender_addr.get('province') or '未知省')[:15],
                'city': (sender_addr.get('city') or '未知市')[:15],
                'district': (sender_addr.get('district') or '')[:15],
                'town': (sender_addr.get('town') or '')[:15],
                'detail': (sender_addr.get('detail') or '未知地址')[:127],
            },
        }
        if sender_mobile:
            sender_obj['mobile'] = str(sender_mobile)[:15]
        if sender_phone:
            sender_obj['phone'] = str(sender_phone)[:15]
        if not sender_obj.get('mobile') and not sender_obj.get('phone'):
            sender_obj['mobile'] = '13800000000'

        recipient_obj = {
            'name': (recipient_name or '收件人')[:31],
            'address': {
                'province': (recipient_addr.get('province') or '未知省')[:15],
                'city': (recipient_addr.get('city') or '未知市')[:15],
                'district': (recipient_addr.get('district') or '')[:15],
                'town': (recipient_addr.get('town') or '')[:15],
                'detail': (recipient_addr.get('detail') or '未知地址')[:127],
            },
        }
        if recipient_mobile:
            recipient_obj['mobile'] = str(recipient_mobile)[:15]
        else:
            recipient_obj['mobile'] = '13800000000'

        return {
            'orderInfo': {
                'orderId': order_id,
                'sender': sender_obj,
                'recipient': recipient_obj,
            },
            'packageInfo': {
                'category': category,
                'weight': 0,
                'volume': 0,
                'note': note or '',
            },
        }

    def build_merchant_trade_from_local(
        self,
        order,
        items: Optional[List[Any]] = None,
        trade_id_suffix: str = '',
    ) -> Dict[str, Any]:
        recipient_addr = validate_recipient_address(getattr(order, 'recipient_address', None) or '')
        created_at = getattr(order, 'created_at', None) or datetime.now()
        updated_at = getattr(order, 'updated_at', None) or created_at
        base_trade_id = str(getattr(order, 'order_number', None) or getattr(order, 'id', '') or uuid.uuid4().hex)
        trade_id = f'{base_trade_id}{trade_id_suffix}'[:31]
        status_map = {
            'pending': 1,
            'confirmed': 1,
            'paid': 1,
            'shipped': 2,
            'completed': 2,
            'cancelled': 3,
            'canceled': 3,
            'closed': 3,
        }
        local_status = str(getattr(order, 'status', '') or 'pending').lower()
        order_lines = []
        selected_items = items if items is not None else (getattr(order, 'items', None) or [])
        for index, item in enumerate(selected_items, start=1):
            product = getattr(item, 'product', None)
            product_id = str(getattr(item, 'product_id', '') or index)
            product_name = _display_order_item_name(item) or f'商品{product_id}'
            quantity = int(getattr(item, 'quantity', 0) or 0) or 1
            unit_price = float(getattr(item, 'unit_price', 0) or 0)
            payment = unit_price * quantity
            order_lines.append({
                'orderId': f'{trade_id}-{index}'[:31],
                'itemCode': product_id[:63],
                'itemName': product_name[:127],
                'itemSkuCode': '',
                'itemSkuName': '',
                'itemImg': '',
                'itemPrice': round(unit_price, 2),
                'itemCount': quantity,
                'payment': round(payment, 2),
                'weight': 0,
                'volume': 0,
            })
        if not order_lines:
            amount = float(getattr(order, 'total_amount', 0) or 0)
            order_lines.append({
                'orderId': f'{trade_id}-1'[:31],
                'itemCode': 'default',
                'itemName': '订单商品',
                'itemSkuCode': '',
                'itemSkuName': '',
                'itemImg': '',
                'itemPrice': round(amount, 2),
                'itemCount': 1,
                'payment': round(amount, 2),
                'weight': 0,
                'volume': 0,
            })
        return {
            'tradeId': trade_id,
            'type': 1,
            'status': status_map.get(local_status, 1),
            'refundStatus': 0,
            'message': '',
            'memo': (getattr(order, 'notes', None) or '')[:255],
            'flag': 0,
            'receiverName': (getattr(order, 'recipient_name', None) or '收件人')[:31],
            'receiverMobile': str(getattr(order, 'recipient_phone', None) or '')[:15],
            'receiverPhone': '',
            'receiverCountry': '中国',
            'receiverProvince': (recipient_addr.get('province') or '未知省')[:15],
            'receiverCity': (recipient_addr.get('city') or '未知市')[:15],
            'receiverArea': (recipient_addr.get('district') or '')[:15],
            'receiverTown': (recipient_addr.get('town') or '')[:15],
            'receiverAddress': (recipient_addr.get('detail') or getattr(order, 'recipient_address', None) or '未知地址')[:127],
            'receiverZip': '',
            'created': created_at.strftime('%Y-%m-%d %H:%M:%S') if hasattr(created_at, 'strftime') else datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'modified': updated_at.strftime('%Y-%m-%d %H:%M:%S') if hasattr(updated_at, 'strftime') else datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'orders': order_lines,
        }

    def push_merchant_trade(self, merchant_id: str, trade: Dict[str, Any]) -> Dict[str, Any]:
        if not merchant_id:
            raise RuntimeError('缺少风火递 merchantId（商户编号）')
        payload = {
            'merchantId': str(merchant_id),
            'trade': trade,
        }
        return self._request('/order/v2/pushMerchantTrade.do', payload)

    def push_local_merchant_trade(
        self,
        order,
        merchant_id: str,
        items: Optional[List[Any]] = None,
        trade_id_suffix: str = '',
    ) -> Dict[str, Any]:
        trade = self.build_merchant_trade_from_local(order, items=items, trade_id_suffix=trade_id_suffix)
        return self.push_merchant_trade(merchant_id, trade)

    def push_orders(self, user_id: str, order_infos: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not user_id:
            raise RuntimeError('缺少风火递 userId（授权用户唯一键）')
        payload = {
            'userId': str(user_id),
            'orderInfos': order_infos,
        }
        return self._request('/order/pushOrders.do', payload)

    def push_local_order(
        self,
        order,
        user_id: Optional[str] = None,
        items: Optional[List[Any]] = None,
        order_id_suffix: str = '',
    ) -> Dict[str, Any]:
        uid = user_id or self.user_id
        info = self.build_order_info_from_local(order, items=items, order_id_suffix=order_id_suffix)
        return self.push_orders(uid, [info])


def verify_callback_sign(psecret: str, form: Dict[str, Any]) -> bool:
    """校验风火递回调签名（发货回调等）。"""
    pid = str(form.get('pid', ''))
    time_ms = str(form.get('time', ''))
    nonce = str(form.get('nonceStr', ''))
    params = str(form.get('params', ''))
    sign = str(form.get('sign', '')).lower()
    expect = build_sign(psecret, pid, int(time_ms or 0), nonce, params).lower()
    return hmac.compare_digest(expect, sign)


def verify_notify_sign(psecret: str, pid: str, timestamp: str, nonce_str: str, body_json: str, sign: str) -> bool:
    """校验订单通知签名（header: pid/timestamp/nonceStr/sign + body JSON）。

    规则：对除 sign 外的公共参数和业务参数 body 排序后拼接，前后加 appsecret，再 HMAC_MD5。
    """
    pairs = {
        'body': str(body_json or ''),
        'nonceStr': str(nonce_str or ''),
        'pid': str(pid or ''),
        'timestamp': str(timestamp or ''),
    }
    items = [f'{k}{pairs[k]}' for k in sorted(pairs.keys())]
    sign_string = f"{psecret}{''.join(items)}{psecret}"
    expect = hmac_md5_hex(psecret, sign_string).lower()
    return hmac.compare_digest(expect, str(sign or '').lower())
