"""
API 客户端
用于 Telegram Bot 调用 FastAPI 后端
"""

import requests
import json
import os
import threading
import logging
import time
from typing import Dict, List, Optional, Tuple
from config import API_BASE_URL, API_SECRET_KEY


class APIClient:
    """API 客户端"""

    def get_bundle_catalog(self):
        ok,result=self._make_request('GET','/api/bundles/catalog')
        return result if ok and isinstance(result,list) else []

    def preview_bundles(self,telegram_id,bundle_requests,items):
        return self._make_request('POST','/api/bundles/preview',data=dict(telegram_id=telegram_id,
            bundle_requests=json.dumps(bundle_requests,ensure_ascii=False),items=json.dumps(items,ensure_ascii=False)))

    def __init__(self):
        self.base_url = API_BASE_URL.rstrip('/')
        self.secret_key = API_SECRET_KEY
        self._sessions = threading.local()
        self._request_slots = threading.BoundedSemaphore(
            max(1, int(os.getenv('BOT_API_MAX_CONCURRENCY', '4')))
        )

    def _http_session(self):
        session = getattr(self._sessions, 'session', None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=2, pool_maxsize=2, max_retries=0, pool_block=True,
            )
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            self._sessions.session = session
        return session

    def _make_request(self, method: str, endpoint: str, data: Dict = None, params: Dict = None) -> Tuple[bool, any]:
        """
        发送 HTTP 请求
        Returns: (是否成功, 响应数据或错误消息)
        """
        from execution_safety import ensure_running
        ensure_running()
        try:
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-API-Key": self.secret_key,
            }

            url = f"{self.base_url}{endpoint}"

            # 同步带上 query api_key，兼容双校验
            params = dict(params or {})
            if 'api_key' not in params:
                params['api_key'] = self.secret_key

            method = method.upper()
            if method == "GET":
                data = None
            elif method == "POST":
                # form 里也带一份，便于部分网关透传
                data = dict(data or {})
                if 'api_key' not in data:
                    data['api_key'] = self.secret_key
                # SQLite writers may legitimately wait up to 30 seconds.
                # A single creation request must cover the same bounded wait
                # as draft persistence. Never retry its POST after uncertainty.
                timeout=35 if endpoint in ('/api/telegram/draft/open','/api/telegram/draft/save','/api/orders/create') else 10
                timeout = (3, timeout)
            else:
                return False, "不支持的请求方法"

            from order_trace import stage
            from contextlib import nullcontext
            with stage('HTTP_CREATE') if endpoint=='/api/orders/create' else nullcontext():
                # Only these proven read-only catalog/stock endpoints may
                # recover a transient connection. Never retry a business POST
                # or an arbitrary GET whose side effects are not established.
                read_retry=method=='GET' and endpoint in ('/api/stock','/api/products','/api/warehouses')
                for attempt in range(2 if read_retry else 1):
                    ensure_running()
                    try:
                        with self._request_slots:
                            response = self._http_session().request(
                                method, url, data=data, params=params, headers=headers,
                                timeout=(3, 10) if method == 'GET' else timeout,
                            )
                        break
                    except (requests.exceptions.ConnectionError,requests.exceptions.Timeout) as error:
                        if not read_retry or attempt:raise
                        logging.getLogger(__name__).warning('API_READ_RETRY endpoint=%s kind=%s attempt=1',endpoint,type(error).__name__)
                        # Release HTTP capacity while backing off; the next
                        # iteration rechecks the execution fence before I/O.
                        time.sleep(.1)

            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' in content_type:
                    return True, response.json()
                else:
                    return True, response.text
            else:
                if response.status_code>=500:
                    from execution_safety import technical_failure
                    technical_failure('HTTP_SERVER_ERROR')
                try:
                    error_data = response.json()
                    return False, error_data.get('detail', f'HTTP错误: {response.status_code}')
                except:
                    return False, f'HTTP错误: {response.status_code}'

        except requests.exceptions.ConnectionError:
            from execution_safety import technical_failure
            technical_failure('HTTP_CONNECTION_ERROR')
            return False, "无法连接到 API 服务器，请检查是否已启动"
        except requests.exceptions.Timeout:
            from execution_safety import technical_failure
            technical_failure('HTTP_TIMEOUT')
            return False, "请求超时"
        except Exception as e:
            return False, f"请求失败: {str(e)}"

    def create_or_get_user(
        self,
        telegram_id: str,
        username: Optional[str] = None,
        telegram_name: Optional[str] = None,
    ) -> Tuple[bool, Optional[Dict]]:
        """创建或获取用户"""
        data = {
            'telegram_id': telegram_id
        }
        if username:
            data['username'] = username
        if telegram_name:
            data['telegram_name'] = telegram_name

        success, response = self._make_request("POST", "/api/users/create_or_get", data=data)
        return success, response if success else None

    def get_user_balance(self, telegram_id: str) -> float:
        """获取用户余额"""
        success, user = self.create_or_get_user(telegram_id)
        if success and user:
            return user.get('balance', 0.0)
        return 0.0

    def get_inventory_image_rows(self) -> List[Dict]:
        """Complete image catalog, including zero-stock products without lots."""
        success, rows = self._make_request("GET", "/api/inventory-image/catalog")
        if not success:
            raise RuntimeError('库存图片目录读取失败')
        return rows

    def get_products(self) -> List[Dict]:
        """获取所有商品"""
        success, products = self._make_request("GET", "/api/products")
        return products if success else []

    def get_product_by_name(self, name: str) -> Optional[Dict]:
        """根据名称获取商品"""
        products = self.get_products()
        for product in products:
            if product['name'] == name and product.get('is_active', True):
                return product
        return None

    def get_product_stock(self, product_id: int) -> int:
        """获取商品库存"""
        success, stock = self._make_request("GET", "/api/stock", params={'product_id': product_id})
        return stock.get('quantity', 0) if success else 0

    def update_stock(self, product_id: int, quantity: int, warehouse_id: int = None,
                     mode: str = "set") -> Tuple[bool, any]:
        """更新库存。

        mode:
          - set: 覆盖库存
          - add: 增量入库
          - subtract: 减少库存（不足时失败）
        """
        data = {
            'product_id': product_id,
            'quantity': quantity,
            'mode': mode,
        }
        if warehouse_id is not None:
            data['warehouse_id'] = warehouse_id

        success, response = self._make_request("POST", "/api/stock/update", data=data)
        if success:
            return True, response
        else:
            return False, response

    def adjust_stock(self, product_id: int, quantity: int, warehouse_id: int = None,
                     operation: str = "add", note: str = None) -> Tuple[bool, any]:
        """Increase or decrease stock by a delta for admin workflows."""
        mode = "add" if str(operation).strip().lower() in {"add", "increase", "in"} else "subtract"
        data = {"product_id": product_id, "quantity": abs(int(quantity)), "mode": mode}
        if warehouse_id is not None:
            data["warehouse_id"] = warehouse_id
        if note:
            data["note"] = note
        return self._make_request("POST", "/api/stock/update", data=data)

    def inbound_stock(self, product_id: int, quantity: int, warehouse_id: int = None,
                      mode: str = "add") -> Tuple[bool, any]:
        """到货入库（默认增量）"""
        return self.update_stock(product_id, quantity, warehouse_id=warehouse_id, mode=mode)

    def get_stock_filtered(self, warehouse_id: int = None, product_id: int = None, include_inactive: bool = False) -> List[Dict]:
        """按仓库/商品过滤库存"""
        params = {}
        if warehouse_id is not None:
            params['warehouse_id'] = warehouse_id
        if product_id is not None:
            params['product_id'] = product_id
        if include_inactive:
            params['include_inactive'] = 'true'
        success, stocks = self._make_request("GET", "/api/stock", params=params or None)
        if not success:
            return []
        # 单个库存记录时 API 返回 dict
        if isinstance(stocks, dict):
            return [stocks]
        return stocks or []

    def check_stock(self, product_id: int, required_quantity: int) -> Tuple[bool, Optional[str]]:
        """
        检查库存是否充足
        Returns: (是否充足, 不足时的商品信息或None)
        """
        success, stock = self._make_request("GET", "/api/stock", params={'product_id': product_id})
        if not success:
            return False, "无法检查库存"

        current_stock = stock.get('quantity', 0)
        product_name = stock.get('product_name', '未知商品')

        if current_stock < required_quantity:
            return False, f"{product_name} (库存: {current_stock}, 需要: {required_quantity})"
        else:
            return True, None

    def create_order(self, telegram_id: str, items: List[Dict], recipient_info: Dict,
                     source_info: Optional[Dict] = None,
                     expected_quote: Optional[Dict] = None,
                     automatic_order: bool = False,
                     cross_warehouse_confirmed: bool = False,
                     explicit_warehouse_id: Optional[int] = None,
                     bundle_requests: Optional[List[Dict]] = None,
                     proxy_info: Optional[Dict] = None) -> Tuple[bool, Dict]:
        """
        创建订单

        Args:
            telegram_id: 用户Telegram ID
            items: 商品列表 [{'product_name': '苹果', 'quantity': 2}, ...]
            recipient_info: 收件人信息 {
                'recipient_name': '张三',
                'recipient_phone': '13800138000',
                'recipient_address': '北京市朝阳区xxx街道xxx号'
            }

        Returns: (是否成功, 订单信息或错误消息)
        """
        # 首先获取或创建用户
        success, user = self.create_or_get_user(telegram_id)
        if not success or not user:
            return False, {"error": "用户不存在"}

        user_id = user['id']

        # 检查商品和库存
        items_json = json.dumps(items)

        # 准备订单数据
        order_data = {
            'telegram_id': telegram_id,
            'items': items_json,
            'recipient_name': recipient_info['recipient_name'],
            'recipient_phone': recipient_info['recipient_phone'],
            'recipient_address': recipient_info['recipient_address']
        }
        from order_trace import CURRENT
        from execution_safety import credentials
        execution_info=credentials()
        if execution_info:
            order_data['execution_credentials']=json.dumps(execution_info)
        trace = CURRENT.get()
        if trace:
            order_data['diagnostic_trace_id'] = trace['trace_id']
        if expected_quote is not None:
            order_data['expected_quote'] = json.dumps(expected_quote, ensure_ascii=False)
        if proxy_info is not None:
            order_data['proxy_confirmation']=json.dumps(proxy_info)
        if bundle_requests is not None:
            order_data['bundle_requests']=json.dumps(bundle_requests,ensure_ascii=False)
        if automatic_order:
            order_data['automatic_order'] = 'true'
        if cross_warehouse_confirmed:
            order_data['cross_warehouse_confirmed'] = 'true'
        if explicit_warehouse_id is not None:
            order_data['explicit_warehouse_id'] = int(explicit_warehouse_id)
        for key in (
            'source_chat_id', 'source_message_id',
            'source_chat_type', 'source_chat_title',
        ):
            value = (source_info or {}).get(key)
            if value is not None and value != '':
                order_data[key] = value

        success, response = self._make_request("POST", "/api/orders/create", data=order_data)

        if success:
            return True, response
        else:
            if response in ('请求超时', '无法连接到 API 服务器，请检查是否已启动'):
                recovered = self._read_committed_source(user_id, telegram_id, source_info,
                                                       recipient_info, items, bundle_requests)
                if recovered is not None:
                    return True, recovered
            return False, {"error": response}

    def _read_committed_source(self, user_id, telegram_id, source_info, recipient_info, items, bundle_requests=None):
        """Reconcile an uncertain write using GETs only, never resend creation.

        Exact source filters avoid losing evidence outside the newest 100 rows.
        A miss is inconclusive. Never expand a bundle from today's configuration.
        """
        source=source_info or {}
        if not source.get('source_chat_id') or not source.get('source_message_id'):
            return None
        # A lost response can precede commit. Poll reads a bounded number of
        # times; an empty result never authorizes another creation POST.
        import time
        for attempt in range(3):
            rows=self.get_orders(user_id=user_id,limit=100,
                                 source_chat_id=str(source['source_chat_id']),
                                 source_message_id=source['source_message_id'])
            if not isinstance(rows,list) or rows:break
            if attempt<2:time.sleep(1)
        if not isinstance(rows,list):return None
        matches=[row for row in rows if isinstance(row,dict)
                 and str(row.get('source_chat_id'))==str(source['source_chat_id'])
                 and row.get('source_message_id')==source['source_message_id']
                 and row.get('user_id')==user_id]
        if len(matches)!=1:return None
        row=matches[0]
        if not row.get('id') or not row.get('order_number'):return None
        if row.get('status')!='pending' or int(row.get('split_count') or 1)!=1 or row.get('split_group_id'):
            return None
        if any(row.get(key)!=recipient_info.get(key) for key in
               ('recipient_name','recipient_phone','recipient_address')):return None
        def counts(values):
            result={}
            for item in values:
                pid=int(item['product_id']);quantity=int(item['quantity'])
                if pid<=0 or quantity<=0:raise ValueError('Invalid persisted item')
                result[pid]=result.get(pid,0)+quantity
            return result
        try:
            persisted = row.get('items') or []
            sales = row.get('bundle_sales') or []
            if bundle_requests:
                # Explicit selections need per-portion evidence; never assume
                # that a matching bundle name proves a customer's selection.
                if any(request.get('selections') for request in bundle_requests):return None
                requested={}
                for request in bundle_requests:
                    bid=int(request['bundle_id']);qty=int(request['quantity'])
                    if bid<=0 or qty<=0:return None
                    requested[bid]=requested.get(bid,0)+qty
                committed={};owned_ids=set()
                actual_by_id={int(item['id']):item for item in persisted}
                if len(actual_by_id)!=len(persisted):return None
                for sale in sales:
                    bid=int(sale['bundle_id']);qty=int(sale['quantity'])
                    ids=[int(value) for value in sale['item_ids']]
                    if qty<=0 or not ids or len(set(ids))!=len(ids) or owned_ids.intersection(ids):return None
                    if int(sale['order_id'])!=int(row['id']):return None
                    if counts(sale['items'])!=counts([actual_by_id[value] for value in ids]):return None
                    if sum(int(item['quantity']) for item in sale['items'])!=int(sale['units'])*qty:return None
                    committed[bid]=committed.get(bid,0)+qty
                    owned_ids.update(ids)
                if requested!=committed:return None
                ordinary=[item for item in persisted if int(item['id']) not in owned_ids]
                if counts(ordinary)!=counts(items):return None
            elif sales or counts(persisted)!=counts(items):return None
            if not persisted:return None
            import math
            if any(not math.isfinite(float(row[key])) or float(row[key])<0
                   for key in ('total_amount','total_shipping_fee','grand_total')):return None
        except (KeyError,ValueError,TypeError):return None
        summary=self.account_summary(telegram_id)
        if not isinstance(summary,dict) or 'pending' not in summary:return None
        try:
            if not math.isfinite(float(summary['pending'])) or float(summary['pending'])<0:return None
        except (ValueError,TypeError):return None
        recovered=dict(row,order_id=row['id'],user_debt=summary['pending'],
                       daily_summary=summary,
                       recovered_after_transport_uncertainty=True,
                       items=[dict(item,parent_name=item.get('category_name')) for item in row['items']])
        return recovered

    def get_orders(self, user_id: int = None, status: str = None, limit: int = 100,
                   offset: int = 0, recorded: Optional[bool] = None,
                   record_status: str = None, start_date: str = None,
                   end_date: str = None, source_chat_id: str = None,
                   source_message_id: int = None) -> List[Dict]:
        """获取订单列表"""
        params = {}
        if user_id:
            params['user_id'] = user_id
        if source_chat_id is not None:
            params['source_chat_id'] = source_chat_id
        if source_message_id is not None:
            params['source_message_id'] = source_message_id
        if status:
            params['status'] = status
        if limit:
            params['limit'] = limit
        if offset:
            params['offset'] = offset
        if recorded is not None:
            params['recorded'] = str(bool(recorded)).lower()
        if record_status:
            params['record_status'] = record_status
        if start_date:
            params['start_date'] = start_date
        if end_date:
            params['end_date'] = end_date

        success, orders = self._make_request("GET", "/api/orders", params=params)
        return orders if success else []

    def get_user_orders(self, telegram_id: str) -> List[Dict]:
        """获取用户订单"""
        # 获取用户信息
        success, user = self.create_or_get_user(telegram_id)
        if not success or not user:
            return []

        return self.get_orders(user_id=user['id'], limit=50)

    def update_order_status(self, order_id: int, status: str, reason: str = None) -> Tuple[bool, any]:
        """更新订单状态"""
        data = {
            'status': status
        }
        if reason:
            data['reason'] = reason

        success, response = self._make_request("POST", f"/api/orders/{order_id}/update_status", data=data)
        if success:
            return True, response
        else:
            return False, response

    def cancel_order(self, order_id: int = None, order_number: str = None,
                     telegram_id: str = None, reason: str = None) -> Tuple[bool, any]:
        """取消订单（回滚库存+退款）"""
        if order_id is not None:
            data = {}
            if reason:
                data['reason'] = reason
            return self._make_request("POST", f"/api/orders/{order_id}/cancel", data=data)

        if order_number:
            data = {'order_number': order_number}
            if telegram_id:
                data['telegram_id'] = telegram_id
            if reason:
                data['reason'] = reason
            return self._make_request("POST", "/api/orders/cancel_by_number", data=data)

        return False, "缺少订单号或订单ID"

    def request_order_cancellation(self, order_number: str, telegram_id: str,
                                   reason: str = None) -> Tuple[bool, any]:
        """Submit a customer cancellation request; approval is required before cancellation."""
        order_number = (order_number or '').strip().lstrip('#')
        telegram_id = str(telegram_id or '').strip()
        if not order_number or not telegram_id:
            return False, "缂哄皯璁㈠崟鍙锋垨 Telegram ID"
        data = {
            'order_number': order_number,
            'telegram_id': telegram_id,
        }
        if reason:
            data['reason'] = reason
        return self._make_request("POST", "/api/orders/cancel_request_by_number", data=data)

    def get_warehouse_stock(self, warehouse_id: int, product_id: int, include_inactive: bool = False) -> Optional[Dict]:
        """获取仓库中某个商品的库存信息"""
        params = {
            'warehouse_id': warehouse_id,
            'product_id': product_id
        }
        if include_inactive:
            params['include_inactive'] = 'true'

        success, stock = self._make_request("GET", "/api/stock", params=params)
        return stock if success else None

    def get_all_warehouses(self) -> List[Dict]:
        """获取所有仓库"""
        success, warehouses = self._make_request("GET", "/api/warehouses")
        return warehouses if success else []

    def get_address_daily_units(self, address: str) -> Optional[int]:
        """这个收货地址「今天」已经下了多少支。查不到返回 None（调用方要放行）。

        快递对单地址每天件数有上限，Bot 在下单预览阶段先问一次，好给客户人话提示；
        真正的闸门在服务端创建订单那里。
        """
        address = str(address or '').strip()
        if not address:
            return 0
        ok, data = self._make_request(
            'GET', '/api/orders/address_usage', params={'address': address},
        )
        if not ok or not isinstance(data, dict):
            return None
        try:
            return int(data.get('used_units') or 0)
        except (TypeError, ValueError):
            return None

    def check_warehouse_stock(self, warehouse_id: int, product_id: int, required_quantity: int) -> Tuple[bool, Optional[str]]:
        """
        检查指定仓库的商品库存是否充足
        Returns: (是否充足, 错误信息)
        """
        stock_info = self.get_warehouse_stock(warehouse_id, product_id, include_inactive=True)
        if not stock_info:
            return False, "商品在该仓库没有库存记录"

        current_stock = stock_info.get('quantity', 0)
        if not stock_info.get('is_active', True):
            product_name = stock_info.get('product_name', '商品')
            return False, f"{product_name} 已下架，不能下单"

        # 用「可售库存」而不是实际库存：预警线在 threshold 模式下是预留量，
        # 只比实际库存的话，一单就能把预留吃穿（见 stock_rules 模块头部说明）。
        import config as cfg
        from stock_rules import sellable_quantity
        available = sellable_quantity(
            current_stock,
            stock_info.get('low_stock_threshold'),
            getattr(cfg, 'STOCK_AUTO_UNLIST_MODE', 'threshold'),
        )
        if available < required_quantity:
            product_name = stock_info.get('product_name', '商品')
            warehouse_name = stock_info.get('warehouse_name', '仓库')
            return False, (
                f"{product_name} 在 {warehouse_name} 可售库存不足"
                f"（可售: {available}, 需要: {required_quantity}）"
            )

        return True, None

    def get_stock(self, include_inactive: bool = False) -> List[Dict]:
        """获取所有仓库的库存信息"""
        params = {'include_inactive': 'true'} if include_inactive else None
        success, stocks = self._make_request("GET", "/api/stock", params=params)
        return stocks if success else []

    def get_user_detail(self, user_id: int) -> Optional[Dict]:
        success, data = self._make_request("GET", f"/api/users/{user_id}")
        return data if success else None

    def get_my_profile(self, telegram_id: str) -> Optional[Dict]:
        success, user = self.create_or_get_user(telegram_id)
        return user if success else None

    def get_user_prices(self, user_id: int) -> Dict:
        success, data = self._make_request("GET", f"/api/users/{user_id}/prices")
        return data if success else {'prices': [], 'debt': 0, 'balance': 0}

    def create_payment(self, user_id: int, amount: float, method: str = 'cny',
                       order_id: int = None, note: str = None) -> Tuple[bool, any]:
        data = {
            'user_id': user_id,
            'amount': amount,
            'method': method,
        }
        if order_id is not None:
            data['order_id'] = order_id
        if note:
            data['note'] = note
        return self._make_request("POST", "/api/payments/create", data=data)

    def daily_stats(self, day: str = None) -> Dict:
        params = {'day': day} if day else None
        success, data = self._make_request("GET", "/api/stats/daily", params=params)
        return data if success else {}

    def sales_breakdown(self, period: str = "day", ref: str = None, shipped_only: bool = False) -> Dict:
        params = {'period': period}
        if ref:
            params['ref'] = ref
        if shipped_only:
            params['shipped_only'] = 'true'
        success, data = self._make_request("GET", "/api/stats/sales_breakdown", params=params)
        return data if success else {}

    def get_order_print(self, order_id: int) -> Optional[Dict]:
        success, data = self._make_request("GET", f"/api/orders/{order_id}/print")
        return data if success else None

    def get_order_by_id(self, order_id: int) -> Optional[Dict]:

        """获取订单详情"""
        success, order = self._make_request("GET", f"/api/orders/{order_id}")
        return order if success else None

    def get_parent_categories(self):
        ok, data = self._make_request("GET", "/api/products/parents")
        if ok and isinstance(data, list):
            return data
        return []

    def apply_dropship_auth(
        self,
        telegram_id: str,
        username: str = None,
        telegram_name: str = None,
        message: str = None,
    ) -> Tuple[bool, Dict]:
        data = {
            "telegram_id": telegram_id,
        }
        if username:
            data["username"] = username
        if telegram_name:
            data["telegram_name"] = telegram_name
        if message:
            data["message"] = message
        success, response = self._make_request("POST", "/api/dropship/apply", data=data)
        if success:
            return True, response
        return False, {"error": response}

    def get_dropship_auth_status(self, telegram_id: str) -> Optional[Dict]:
        """Return the latest group-order authorization state for a Telegram user."""
        success, response = self._make_request(
            "GET", "/api/dropship/status", params={"telegram_id": str(telegram_id)}
        )
        return response if success and isinstance(response, dict) else None

    def get_telegram_account(self, telegram_id='',chat_id=''):
        success,result=self._make_request('GET','/api/telegram/account',params=dict(telegram_id=str(telegram_id),chat_id=str(chat_id)))
        self._check_read_transport(success,result)
        return result if success and isinstance(result,dict) else None

    @staticmethod
    def _check_read_transport(success,result):
        if not success and (result in ('请求超时','无法连接到 API 服务器，请检查是否已启动')
                or isinstance(result,str) and result.startswith(('HTTP错误: 5','请求失败:'))):
            raise RuntimeError('READ_API_TRANSPORT_UNCERTAIN')

    def claim_group_message(self,chat_id,message_id,sender_id):
        success,result=self._make_request('POST','/api/telegram/message/claim',data=dict(chat_id=chat_id,message_id=message_id,sender_id=sender_id))
        return bool(success and isinstance(result,dict) and result.get('claimed'))

    def account_summary(self,telegram_id):
        success,result=self._make_request('GET','/api/telegram/account/summary',params=dict(telegram_id=str(telegram_id)))
        return result if success else None

    def account_order_search(self,telegram_id,keyword='',offset=0):
        success,result=self._make_request('GET','/api/telegram/account/orders',params=dict(telegram_id=str(telegram_id),keyword=keyword,offset=offset))
        return result if success else None

    def open_group_draft(self,chat_id,message_id,sender_id,new_order=False,reply_message_id=None,resume=False):
        data=dict(chat_id=chat_id,message_id=message_id,sender_id=sender_id,new_order=str(bool(new_order)).lower())
        if reply_message_id is not None:data['reply_message_id']=reply_message_id
        if resume:data['resume']='true'
        success,result=self._make_request('POST','/api/telegram/draft/open',data=data)
        return success,result

    def save_group_draft(self,chat_id,token,payload,completed=False,sender_id=None):
        encoded=json.dumps(payload,ensure_ascii=False)
        success,result=self._make_request('POST','/api/telegram/draft/save',data=dict(chat_id=chat_id,token=token,
            payload=encoded,completed=str(bool(completed)).lower(),sender_id=sender_id))
        if not success and result in ('请求超时','无法连接到 API 服务器，请检查是否已启动') and sender_id:
            import hashlib
            # Hash the exact JSON snapshot sent, not a possibly changed live
            # user_data dict. One POST only; uncertainty allows only a GET.
            digest=hashlib.sha256(json.dumps(json.loads(encoded),ensure_ascii=False,
                sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
            verified,proof=self._make_request('GET','/api/telegram/draft/save-result',params=dict(
                chat_id=str(chat_id),token=token,sender_id=str(sender_id),payload_sha256=digest,
                completed=str(bool(completed)).lower()))
            if verified and isinstance(proof,dict) and proof.get('saved') is True:
                return True,dict(saved=True,recovered_after_transport_uncertainty=True)
        return success,result

    def get_group_draft(self,chat_id,token,sender_id=None,**audit):
        params=dict(chat_id=str(chat_id),token=token,sender_id=sender_id)
        params.update({k:v for k,v in audit.items() if k in ('audit_event_id','audit_action','audit_message_id','include_flavor_expired')})
        success,result=self._make_request('GET','/api/telegram/draft',params=params)
        self._check_read_transport(success,result)
        return result if success else None

    def get_proxy_confirmation_draft(self,chat_id,token,sender_id):
        success,result=self._make_request('GET','/api/telegram/proxy-confirmation-draft',params=dict(chat_id=str(chat_id),token=token,sender_id=str(sender_id)))
        self._check_read_transport(success,result)
        return result if success else None


# 全局 API 客户端实例
api_client = APIClient()
