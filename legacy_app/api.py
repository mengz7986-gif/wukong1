"""
FastAPI 后端服务
提供 REST API 和管理界面
"""

from fastapi import FastAPI, Depends, HTTPException, Request, Form, Query, UploadFile, File, Header
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool
from sqlalchemy import and_, case, func, or_, inspect as inspect_database, text as sql_text, bindparam
from sqlalchemy.orm import Session, aliased, selectinload
from sqlalchemy.exc import OperationalError
from typing import List, Optional, Annotated
import asyncio
import uvicorn
from datetime import datetime
from business_time_range import business_format,business_now
import uuid
import os
import tempfile
from pathlib import Path
import json
import secrets
import html
import logging
import time
import re
import functools
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from pydantic import BaseModel, StrictInt, BeforeValidator
from sale_price_review import amount as validate_sale_amount

# Validate original input before FastAPI/Pydantic float coercion (e.g. '1e2').
SaleAmount = Annotated[float, BeforeValidator(validate_sale_amount)]

def _sale_form_amount(value):
    if value is None:
        return None
    try:
        return validate_sale_amount(value)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None

from models import (
    db, User, Product, Warehouse, WarehouseStock, Order, OrderItem, Transaction,
    UserProductPrice, UserCategoryPrice, Payment, StockMovement, InventoryBatch, BlockedAddress,
    FenghuodiPushLog, DropshipAuthRequest, OrderCancellationRequest, normalize_low_stock_threshold,
    should_auto_unlist, OrderExportBatch, OrderExportItem,
)
from order_parser import OrderParser

logger = logging.getLogger(__name__)


def _run_blocking_endpoint(function):
    """Keep synchronous SQLAlchemy handlers off the FastAPI event loop."""
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        return await run_in_threadpool(function, *args, **kwargs)
    return wrapper

_usdt_rate_cache = {}


def _json_path_value(payload, path: str):
    """Read a simple dotted JSON path without evaluating provider content."""
    value = payload
    for key in [part for part in str(path or '').split('.') if part]:
        if isinstance(value, dict):
            if key not in value:
                raise ValueError(f'汇率数据缺少字段: {path}')
            value = value[key]
        elif isinstance(value, list) and key.isdigit():
            index = int(key)
            if index >= len(value):
                raise ValueError(f'汇率数据缺少字段: {path}')
            value = value[index]
        else:
            raise ValueError(f'汇率数据缺少字段: {path}')
    return value


def _fetch_usdt_cny_rate_uncached() -> dict:
    """Fetch and validate the configured USDT/CNY rate source.

    An operator override is intentionally opt-in and explicit.  It is useful
    when settlement uses a private/negotiated rate that is not represented by
    public spot feeds; it is never used as an implicit fallback.
    """
    import urllib.request
    import config as cfg

    override = str(getattr(cfg, 'USDT_CNY_RATE_OVERRIDE', '') or '').strip()
    if override:
        try:
            rate = float(override)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'USDT/CNY 运营汇率配置无效: {override}') from exc
        if not (0.01 <= rate <= 1000):
            raise ValueError(f'USDT/CNY 运营汇率异常: {rate}')
        return {
            'rate': rate,
            'fetched_at': time.time(),
            'source': 'configured_override',
        }

    url = str(getattr(cfg, 'USDT_CNY_RATE_URL', '') or '').strip()
    if not url:
        raise ValueError('未配置 USDT/CNY 实时汇率地址')
    timeout = float(getattr(cfg, 'USDT_CNY_RATE_TIMEOUT_SECONDS', 6) or 6)
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'BopaiOrderBot/1.0',
    }
    request_data = None
    source = 'configured_provider'
    if 'p2p.binance.com' in url.lower():
        import config as cfg
        request_data = json.dumps({
            'fiat': 'CNY',
            'asset': 'USDT',
            'tradeType': getattr(cfg, 'USDT_CNY_RATE_TRADE_TYPE', 'BUY') or 'BUY',
            'page': 1,
            'rows': 10,
            'payTypes': [],
            'publisherType': None,
            'merchantCheck': False,
        }).encode('utf-8')
        headers['Content-Type'] = 'application/json'
        source = 'binance_p2p'
    request = urllib.request.Request(url, data=request_data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
        payload = json.loads(raw.decode('utf-8'))
        if source == 'binance_p2p':
            if str(payload.get('code', '')) not in ('000000', '0'):
                raise ValueError(f"Binance P2P 返回错误: {payload.get('message') or payload.get('code')}")
            if not payload.get('data'):
                raise ValueError('Binance P2P 暂无可用 USDT/CNY 广告')
        rate = float(_json_path_value(
            payload,
            getattr(cfg, 'USDT_CNY_RATE_JSON_PATH', 'tether.cny'),
        ))
    except Exception as exc:
        raise ValueError(f'USDT/CNY 实时汇率获取失败: {exc}') from exc
    if not (0.01 <= rate <= 1000):
        raise ValueError(f'USDT/CNY 实时汇率异常: {rate}')
    return {
        'rate': rate,
        'fetched_at': time.time(),
        'source': source,
    }


async def resolve_usdt_cny_rate(force: bool = False) -> dict:
    """Return a short-lived live rate; never fall back to a hard-coded price."""
    import config as cfg

    ttl = int(getattr(cfg, 'USDT_CNY_RATE_TTL_SECONDS', 60) or 60)
    cached = dict(_usdt_rate_cache)
    now = time.time()
    if (
        not force
        and cached.get('rate')
        and now - float(cached.get('fetched_at') or 0) < ttl
    ):
        cached['cached'] = True
        return cached
    fresh = await asyncio.to_thread(_fetch_usdt_cny_rate_uncached)
    _usdt_rate_cache.clear()
    _usdt_rate_cache.update(fresh)
    return {**fresh, 'cached': False}


# 停仓规则（按仓库）。顶层 import：漏部署要在启动时就报错，
# 而不是等到后台点开关或客户下单才发现。
import sales_pause
# 数量/包裹限额（单笔上限、每 N 支一包、同一地址当天上限）。同样放顶层，
# 漏部署要在启动时就报错。
import package_rules
from address_utils import normalize_cn_address
from config import (
    API_PORT, API_HOST, API_SECRET_KEY,
    PRODUCTS, INITIAL_STOCK, TELEGRAM_BOT_TOKEN,
    ADMIN_USERNAME, ADMIN_PASSWORD, SESSION_SECRET_KEY,
    SHIPPING_FEE_AMOUNT, SHIPPING_NORMAL_QTY, SHIPPING_POD_QTY,
    SHIPPING_POD_KEYWORDS, SHIPPING_MODE,
)

WHOLESALE_PRIVATE_WAREHOUSE_NAME = os.getenv('WHOLESALE_PRIVATE_WAREHOUSE_NAME', '旺财仓').strip()
WHOLESALE_PRIVATE_USERNAME = os.getenv('WHOLESALE_PRIVATE_USERNAME', 'xiaolin005').strip().lstrip('@').casefold()


def _username_token(value) -> str:
    return str(value or '').strip().lstrip('@').casefold()


def _is_private_warehouse(warehouse: Warehouse) -> bool:
    return bool(
        WHOLESALE_PRIVATE_WAREHOUSE_NAME
        and warehouse
        and str(getattr(warehouse, 'name', '') or '').strip() == WHOLESALE_PRIVATE_WAREHOUSE_NAME
    )


def _ensure_order_warehouse_access(user: User, warehouse: Warehouse, product: Product):
    """服务端最终校验商品绑定仓及旺财仓账号权限。"""
    # 先校验私仓权限，避免未授权用户通过错误商品绑定状态得到内部细节。
    if _is_private_warehouse(warehouse) and _username_token(getattr(user, 'username', '')) != WHOLESALE_PRIVATE_USERNAME:
        raise HTTPException(status_code=403, detail='旺财仓仅限指定账号下单')
    if int(getattr(product, 'default_warehouse_id', 0) or 0) != int(warehouse.id):
        raise HTTPException(status_code=400, detail=f'商品只能从绑定仓库发货：{product.get_full_name()}')

# 创建 FastAPI 应用
app = FastAPI(
    title="彩虹仓 API",
    description="Telegram自动下单机器人的后端服务",
    version="1.0.0"
)

# 挂载静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")

# 模板引擎
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="tg_admin_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 12,
)
from web_security import WebBoundary
app.add_middleware(WebBoundary)


async def _pending_notification_worker():
    from account_notifications import drain_pending
    while True:
        try:
            from flavor_draft_expiry import tick as expire_flavor_drafts
            await asyncio.to_thread(expire_flavor_drafts, db)
            from split_management import tick, sync_messages
            await asyncio.to_thread(tick,db)
            await sync_messages(db,_split_telegram_transport)
            await drain_pending(db,send_telegram_message_async)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('未发送通知事件恢复失败；不会重复执行账务')
        await asyncio.sleep(15)


async def _split_telegram_transport(action,chat_id,message_id,payload):
    from telegram import Bot
    from telegram.error import BadRequest
    import config
    async with Bot(config.BOT_TOKEN) as bot:
        try:
            if action=='send':return (await bot.send_message(chat_id=chat_id,text=payload)).message_id
            if action=='edit':return await bot.edit_message_text(chat_id=chat_id,message_id=message_id,text=payload)
            return await bot.delete_message(chat_id=chat_id,message_id=message_id)
        except BadRequest as exc:
            description=str(exc).lower()
            if action=='delete' and ('message to delete not found' in description):return True
            if action=='edit' and 'message is not modified' in description:return True
            raise


async def _sync_split_notices():
    try:
        from split_management import sync_messages
        await sync_messages(db,_split_telegram_transport)
    except Exception:
        logger.error('分单提醒同步失败；订单事务不受影响')


@app.on_event('startup')
async def start_pending_notifications():
    app.state.organization_task=asyncio.create_task(_organization_worker())
    app.state.failure_analysis_task = (asyncio.create_task(_failure_analysis_worker())
        if os.getenv('RAINBOW_FAILURE_ANALYSIS_ENABLED','0') == '1' else None)
    if os.getenv('RAINBOW_DISABLE_BACKGROUND_WORKERS')=='1':
        app.state.notification_task=None;app.state.bill_batch_task=None;app.state.learning_task=None
        return
    app.state.notification_task=asyncio.create_task(_pending_notification_worker())
    app.state.bill_batch_task=asyncio.create_task(_bill_batch_worker())
    app.state.learning_task=asyncio.create_task(_daily_learning_worker())


async def _organization_worker():
    """Bounded local bookkeeping only; never invokes providers or orders."""
    while True:
        await asyncio.sleep(10)
        def run():
            import deployment_drain
            handle=deployment_drain.begin_activity()
            if handle is None:return
            try:
                from review_organization import organize
                organize(_rules_catalog(),limit=10)
            finally:deployment_drain.end_activity(handle)
        try:await asyncio.to_thread(run)
        except asyncio.CancelledError:raise
        except Exception:logger.warning('本地识别归类暂未完成，将稍后重试')


async def _failure_analysis_worker():
    """Independent read-only provider queue. Never enters order execution."""
    while True:
        await asyncio.sleep(15)
        try:
            from failure_analysis import process_next
            def busy():
                with db.SessionLocal() as session:
                    return bool(session.execute(sql_text("SELECT count(*) FROM telegram_order_jobs WHERE state IN ('RECEIVED','PROCESSING')")).scalar())
            if await asyncio.to_thread(busy):continue
            from ai_transport import run_provider_worker
            await run_provider_worker(process_next, _rules_catalog())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('FAILURE_ANALYSIS_DEFERRED type=%s', type(error).__name__)

async def _daily_learning_worker():
    from learning_daily_service import run_daily,deepseek_review
    from learning_knowledge import daily_review
    from sqlalchemy import text
    while True:
        # Sleep first: never compete with startup migrations/order recovery.
        await asyncio.sleep(60)
        try:
            if os.getenv('RAINBOW_DAILY_LEARNING_ENABLED','1')!='1':continue
            def busy():
                with db.SessionLocal() as session:
                    return bool(session.execute(text("SELECT count(*) FROM telegram_order_jobs WHERE state IN ('RECEIVED','PROCESSING')")).scalar())
            if await asyncio.to_thread(busy):continue
            from datetime import timezone,timedelta
            if datetime.now(timezone(timedelta(hours=8))).hour<18:continue
            await asyncio.to_thread(daily_review)
            await asyncio.to_thread(run_daily,deepseek_review,is_idle=lambda:not busy())
        except asyncio.CancelledError:raise
        except Exception as exc:
            from learning_daily_service import classify_failure
            logger.warning('DAILY_LEARNING_DEFERRED type=%s category=%s',type(exc).__name__,classify_failure(exc))

async def _bill_batch_worker():
    from bill_batches import drain_one
    while True:
        try:
            if os.getenv('BILL_BATCH_SENDING_ENABLED','1')=='1':
                await drain_one(db,send_telegram_message_async)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error('账单批次发送失败；保留通知事实，不修改账务')
        await asyncio.sleep(5)


@app.on_event('shutdown')
async def stop_pending_notifications():
    organization=getattr(app.state,'organization_task',None)
    if organization:
        organization.cancel()
        try:await organization
        except asyncio.CancelledError:pass
    failure = getattr(app.state,'failure_analysis_task',None)
    if failure:
        failure.cancel()
        try:await failure
        except asyncio.CancelledError:pass
    learning=getattr(app.state,'learning_task',None)
    if learning:
        learning.cancel()
        try:await learning
        except asyncio.CancelledError:pass
    batch=getattr(app.state,'bill_batch_task',None)
    if batch:
        batch.cancel()
        try:await batch
        except asyncio.CancelledError:pass
    task=getattr(app.state,'notification_task',None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


from ai_admin import make_router as make_ai_router
def ai_session_security_ready():
    return (len(SESSION_SECRET_KEY) >= 32 and SESSION_SECRET_KEY != 'SESSION_SECRET_KEY'
            and ADMIN_PASSWORD != 'ADMIN_PASSWORD')

app.include_router(make_ai_router(
    templates, lambda: ADMIN_USERNAME,
    ai_session_security_ready,
    catalog=lambda: _rules_catalog(),
))
from price_override_admin import make_router as make_price_override_router
app.include_router(make_price_override_router(db,lambda:ADMIN_USERNAME,ai_session_security_ready))
from product_units import make_router as make_unit_router
app.include_router(make_unit_router(db,lambda:ADMIN_USERNAME,ai_session_security_ready))
from proxy_confirmation import make_admin_router as make_proxy_admin_router
app.include_router(make_proxy_admin_router(db,lambda:ADMIN_USERNAME,ai_session_security_ready))


def _rules_catalog():
    with db.SessionLocal() as session:
        return [dict(id=p.id, name=p.name, parent_name=p.parent.name if p.parent else '',
                     parent_id=p.parent_id, default_warehouse_id=p.default_warehouse_id,
                     warehouse_name=p.default_warehouse.name if p.default_warehouse else '')
                for p in session.query(Product).filter(
                    Product.is_parent == False,
                    Product.is_active == True,
                    Product.is_archived == False,
                    Product.parent.has(Product.is_parent == True),
                    Product.parent.has(Product.is_archived == False),
                    Product.parent.has(Product.is_active == True),
                    Product.parent.has(Product.name != ''),
                ).all() if p.parent.name.strip()]


def _rules_categories():
    # The real category is the standard object; never seed a parallel dictionary.
    with db.SessionLocal() as session:
        return [dict(id=p.id,name=p.name,warehouse_id=p.default_warehouse_id,
                     warehouse_name=p.default_warehouse.name if p.default_warehouse else '')
                for p in session.query(Product).filter_by(is_parent=True,is_archived=False)
                if p.name and p.name.strip()]


from rules_admin import make_router as make_rules_router
from shipping_admin import make_router as make_shipping_router
from admin_security import ordinary_business_ready
app.include_router(make_shipping_router(db, lambda: ADMIN_USERNAME,
    lambda: ordinary_business_ready(SESSION_SECRET_KEY)))
from bundle_admin import make_router as make_bundle_router
app.include_router(make_bundle_router(db,templates,lambda: ADMIN_USERNAME,
    lambda: ordinary_business_ready(SESSION_SECRET_KEY),_rules_catalog))

def _rules_bundle_catalog():
    import bundles
    with db.SessionLocal() as session:
        return bundles.public_catalog(session, reviewed=False)


app.include_router(make_rules_router(
    templates, lambda: ADMIN_USERNAME, _rules_catalog,
    lambda: ordinary_business_ready(SESSION_SECRET_KEY),
    categories=_rules_categories,
    bundle_catalog=_rules_bundle_catalog,
))


def is_admin_logged_in(request: Request) -> bool:
    return bool(request.session.get("admin_logged_in"))


class BatchInboundItem(BaseModel):
    line_no: Optional[int] = None
    category_name: Optional[str] = None
    product_name: Optional[str] = None
    warehouse_name: Optional[str] = None
    quantity: int
    unit_cost: Optional[float] = None
    note: Optional[str] = None
    product_id: Optional[int] = None
    warehouse_id: Optional[int] = None


class BatchInboundRequest(BaseModel):
    items: List[BatchInboundItem]


class BatchProductItem(BaseModel):
    line_no: Optional[int] = None
    category_name: Optional[str] = None
    product_name: str
    warehouse_name: Optional[str] = None
    initial_stock: StrictInt = 0
    low_stock_threshold: Optional[int] = None
    description: Optional[str] = None
    warehouse_id: Optional[int] = None
    parent_id: Optional[int] = None


class BatchProductRequest(BaseModel):
    items: List[BatchProductItem] = []
    text: Optional[str] = None
    parent_id: Optional[int] = None
    warehouse_id: Optional[int] = None
    preview_token: Optional[str] = None


class UserPriceBatchItem(BaseModel):
    product_id: int
    unit_price: SaleAmount


class UserPriceBatchRequest(BaseModel):
    prices: List[UserPriceBatchItem]
    canonical_all_warehouses: bool = False
    expected_product_ids: Optional[List[StrictInt]] = None
    price_review_token: Optional[str] = None


def require_admin_session(request: Request):
    if is_admin_logged_in(request):
        return None
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={next_url}", status_code=302)


def require_admin_or_api_secret(
    request: Request,
    api_key: Optional[str] = Query(None, alias="api_key"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    if is_admin_logged_in(request):
        return "session"
    header_key = x_api_key or request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    key = header_key or api_key
    if key and key == API_SECRET_KEY:
        return "api_key"
    raise HTTPException(status_code=401, detail="未登录或API密钥无效")


def reject_api_key_direct_cancel(auth_source: str):
    """API 密钥只允许机器人提交取消申请，不能绕过后台直接取消订单。"""
    if auth_source == "api_key":
        raise HTTPException(status_code=403, detail="取消订单必须先提交申请，等待后台审核")


def require_cancellation_reason(reason):
    reason = str(reason or '').strip()
    if not reason:
        raise HTTPException(status_code=400, detail='请输入本次取消原因')
    if len(reason) > 2000:
        raise HTTPException(status_code=400, detail='取消原因不能超过2000字')
    return reason


# 依赖注入

def send_telegram_message(
    chat_id: str,
    text: str,
    parse_mode: str = 'HTML',
    reply_to_message_id: int = None,
):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN in {'YOUR_BOT_TOKEN_HERE', 'TOKEN', 'BOT_TOKEN'}:
        return False, 'BOT token missing'
    if not chat_id:
        return False, 'chat_id missing'
    try:
        import requests
        payload = {
            'chat_id': str(chat_id),
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': True,
        }
        if reply_to_message_id is not None:
            payload['reply_to_message_id'] = int(reply_to_message_id)
        from telegram_delivery_sync import send_message
        return send_message(TELEGRAM_BOT_TOKEN,payload,transport=requests.post)
    except Exception as e:
        return False, 'telegram_delivery_' + type(e).__name__


async def send_telegram_message_async(
    chat_id: str,
    text: str,
    parse_mode: str = 'HTML',
    reply_to_message_id: int = None,
):
    """Run the legacy synchronous Telegram client without blocking asyncio."""
    return await asyncio.to_thread(
        send_telegram_message, chat_id, text, parse_mode, reply_to_message_id
    )


def _display_order_item_name(item) -> str:
    """Return the category and flavor for labels, slips, and shipping notices."""
    product = getattr(item, 'product', None)
    product_name = str(getattr(product, 'name', None) or getattr(item, 'product_id', '') or '')
    parent = getattr(product, 'parent', None)
    parent_name = str(getattr(parent, 'name', None) or '').strip()
    if parent_name and parent_name != product_name:
        return f'{parent_name}/{product_name}'
    return product_name


def _group_order_items(items) -> List[dict]:
    """按品类聚合口味，供后台列表、面单和 CSV 共用。"""
    groups = {}
    for item in items or []:
        if isinstance(item, dict):
            category = str(item.get('category_name') or item.get('parent_name') or '默认分类')
            flavor = str(item.get('product_name') or item.get('name') or '-')
            quantity = int(item.get('quantity') or 0)
            unit_price = float(item.get('unit_price') or 0)
            warehouse = str(item.get('warehouse_name') or '')
            unit=item.get('unit','支')
        else:
            product = getattr(item, 'product', None)
            parent = getattr(product, 'parent', None)
            category = str(getattr(parent, 'name', None) or '默认分类')
            flavor = str(getattr(product, 'name', None) or getattr(item, 'product_id', '-') or '-')
            quantity = int(getattr(item, 'quantity', 0) or 0)
            unit_price = float(getattr(item, 'unit_price', 0) or 0)
            warehouse = str(getattr(getattr(item, 'warehouse', None), 'name', None) or '')
            from sqlalchemy.orm import object_session
            item_session=object_session(item) if hasattr(item,'_sa_instance_state') else None
            unit=__import__('product_units').historical(item_session,item) if item_session else '支'
        group = groups.setdefault(category, {
            'category_name': category,
            'flavors': {},
            'quantity': 0,
            'amount': 0.0,
            'warehouses': [],
        })
        flavor_row = group['flavors'].setdefault((flavor,unit), {
            'product_name': flavor,
            'quantity': 0,
            'unit': unit,
            'amount': 0.0,
        })
        flavor_row['quantity'] += quantity
        flavor_row['amount'] += quantity * unit_price
        group['quantity'] += quantity
        group['amount'] += quantity * unit_price
        if warehouse and warehouse not in group['warehouses']:
            group['warehouses'].append(warehouse)
    result = []
    for group in groups.values():
        flavors = list(group['flavors'].values())
        group['flavors'] = flavors
        group['text'] = (
            f"{group['category_name']}："
            + '、'.join(f"{row['product_name']}×{row['quantity']}"+(row['unit'] if row['unit']!='支' else '') for row in flavors)
        )
        group['amount'] = round(group['amount'], 2)
        result.append(group)
    return result


_FHD_SPLIT_TRADE_RE = re.compile(r'^(?P<base>.+?)[-_](?:P|W|Q|S)\d+$', re.IGNORECASE)


def _waybill_values(value):
    """Normalize one or more provider waybill fields to a de-duplicated list."""
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ('waybillCode', 'wlbCode', 'waybill', 'mailNo', 'code'):
            if value.get(key):
                return _waybill_values(value.get(key))
        return []
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_waybill_values(item))
    else:
        values = [part.strip() for part in re.split(r'[,，;；|]', str(value)) if part and part.strip()]
    result = []
    for item in values:
        item = str(item).strip()
        if item and item not in result:
            result.append(item)
    return result


def _fhd_waybill_codes(order: Order, session: Optional[Session] = None) -> List[str]:
    """Return all successful package waybills, falling back to the order field."""
    codes = _waybill_values(getattr(order, 'fhd_waybill_code', None))
    # Every successful push/callback writes the first code to Order for
    # backwards compatibility. Avoid an extra query for orders with no code;
    # this also keeps list endpoints efficient for the overwhelmingly common
    # not-yet-shipped case.
    if session is not None and getattr(order, 'id', None) and (
        codes or getattr(session, '_fhd_waybill_cache', None) is not None
    ):
        try:
            _prime_fhd_waybill_cache(session, [order])
            cache = getattr(session, '_fhd_waybill_cache', {})
            for code in cache.get(order.id, []):
                if code not in codes:
                    codes.append(code)
        except Exception:
            # Notification rendering must still work when called with a light-weight
            # test double or during a transient database failure.
            pass
    return codes


def _fhd_trade_id_candidates(trade_id: str = '', order_id_list=None) -> List[str]:
    """Build exact and base IDs used by FHD callbacks for split orders."""
    values = []
    raw_values = [trade_id]
    if isinstance(order_id_list, (list, tuple, set)):
        raw_values.extend(order_id_list)
    elif order_id_list:
        raw_values.append(order_id_list)
    for raw in raw_values:
        value = str(raw or '').strip()
        if not value or value in values:
            continue
        values.append(value)
        match = _FHD_SPLIT_TRADE_RE.match(value)
        if match and match.group('base') not in values:
            values.append(match.group('base'))
    return values


def _find_order_for_fhd_callback(session: Session, trade_id: str = '', order_id_list=None):
    """Resolve a callback to the parent local order, including split trade IDs."""
    candidates = _fhd_trade_id_candidates(trade_id, order_id_list)
    for candidate in candidates:
        order = session.query(Order).filter(Order.order_number == candidate).first()
        if order:
            return order
        if candidate.isdigit():
            order = session.query(Order).filter(Order.id == int(candidate)).first()
            if order:
                return order
        # A provider may send a generated orderId while the initial push log
        # retained the mapping to the parent local order.
        try:
            log = (
                session.query(FenghuodiPushLog)
                .filter(FenghuodiPushLog.trade_id == candidate)
                .order_by(FenghuodiPushLog.id.desc())
                .first()
            )
            if log and getattr(log, 'order_id', None):
                order = session.query(Order).filter(Order.id == log.order_id).first()
                if order:
                    return order
        except Exception:
            pass
    return None


def build_shipping_notify_text(order: Order, session: Optional[Session] = None) -> str:
    items = []
    for item in order.items:
        wh = item.warehouse.name if item.warehouse else ''
        name = _display_order_item_name(item)
        extra = f' [{wh}]' if wh else ''
        unit=__import__('product_units').item_unit(item)
        items.append(f'- {name} x{item.quantity}{unit}{extra}')
    item_text = chr(10).join(items) if items else '- (no items)'
    due = max(0.0, (order.grand_total or 0) - (getattr(order, 'paid_amount', 0) or 0))

    # 快递单号存在订单的 fhd_waybill_code 上（手填发货和风火递回调写的是同一个字段）。
    # 注意：waybill_code 是 FenghuodiPushLog 的字段，不是 Order 的 —— 拆包时每个包裹一条，
    # 订单上只留最后一个号，要看全部得查 FenghuodiPushLog。
    waybills = _fhd_waybill_codes(order, session)
    waybill_line = f'快递单号: {", ".join(waybills)}' + chr(10) if waybills else ''

    return (
        '订单已发货' + chr(10) + chr(10) +
        f'订单号: #{order.order_number}' + chr(10) +
        '商品:' + chr(10) + item_text + chr(10) + chr(10) +
        f'收件人: {order.recipient_name}' + chr(10) +
        f'电话: {order.recipient_phone}' + chr(10) +
        f'地址: {order.recipient_address}' + chr(10) +
        waybill_line +
        chr(10) +
        f'应付: {float(order.grand_total or 0):.2f}' + chr(10) +
        f'已收: {float(getattr(order, "paid_amount", 0) or 0):.2f}' + chr(10) +
        f'待收: {due:.2f}'
    )


def build_new_order_notify_text(order: Order) -> str:
    """发给客服的新订单通知。用户填的字段全部 HTML 转义，别让地址里的 < 把消息弄坏。"""
    items = []
    for item in getattr(order, "items", None) or []:
        product = getattr(item, "product", None)
        name = product.get_full_name() if product and hasattr(product, "get_full_name") else (
            getattr(product, "name", None) or str(getattr(item, "product_id", ""))
        )
        warehouse = getattr(getattr(item, "warehouse", None), "name", None)
        suffix = f" [{warehouse}]" if warehouse else ""
        items.append(
            f"- {html.escape(str(name))} x{int(getattr(item, 'quantity', 0) or 0)}{__import__('product_units').item_unit(item)}"
            f"{html.escape(str(suffix))}"
        )
    item_text = chr(10).join(items) if items else "- 无商品明细"

    user = getattr(order, "user", None)
    customer = (
        getattr(user, "display_name", None)
        or getattr(user, "telegram_name", None)
        or getattr(user, "username", None)
        or getattr(user, "telegram_id", None)
        or "未知客户"
    )
    nl = chr(10)
    return (
        "🆕 <b>收到新订单</b>" + nl + nl
        + f"<b>订单ID：</b>{html.escape(str(getattr(order, 'order_number', '') or ''))}" + nl
        + f"<b>客户：</b>{html.escape(str(customer))}" + nl
        + f"<b>客户 TG：</b>{html.escape(str(getattr(user, 'telegram_id', '') or '未记录'))}" + nl
        + f"<b>收件人：</b>{html.escape(str(getattr(order, 'recipient_name', '') or ''))}" + nl
        + f"<b>手机号：</b>{html.escape(str(getattr(order, 'recipient_phone', '') or ''))}" + nl
        + f"<b>地址：</b>{html.escape(str(getattr(order, 'recipient_address', '') or ''))}" + nl + nl
        + "<b>商品明细：</b>" + nl + item_text + nl + nl
        + f"<b>商品金额：</b>¥{float(getattr(order, 'total_amount', 0) or 0):.2f}" + nl
        + f"<b>运费：</b>¥{float(getattr(order, 'total_shipping_fee', 0) or 0):.2f}" + nl
        + f"<b>应付总额：</b>¥{float(getattr(order, 'grand_total', 0) or 0):.2f}" + nl
        + f"<b>状态：</b>{html.escape(str(getattr(order, 'status', '') or 'pending'))}"
    )


def resolve_order_notification_targets(session: Session):
    """解析新订单通知的 chat_id。

    顺序：显式配置的 ID → 按数据库用户名查（客服可能换号）→ 都没有就退到管理员。
    机器人**不能**只凭用户名主动私聊，所以最终一定要拿到数字 chat_id。
    总开关关闭时直接返回空列表（.24 就是这种情况）。
    """
    import config as cfg
    if not getattr(cfg, 'ORDER_NOTIFY_ENABLED', False):
        return []
    targets = [
        str(chat_id).strip()
        for chat_id in getattr(cfg, 'ORDER_NOTIFICATION_TELEGRAM_IDS', []) or []
        if str(chat_id).strip()
    ]
    for username in getattr(cfg, 'ORDER_NOTIFICATION_USERNAMES', []) or []:
        normalized = str(username or '').strip().lstrip('@').lower()
        if not normalized:
            continue
        user = session.query(User).filter(
            func.lower(User.username) == normalized,
            User.is_active == True,
        ).first()
        if user and user.telegram_id:
            chat_id = str(user.telegram_id).strip()
            if chat_id and chat_id not in targets:
                targets.append(chat_id)
    if not targets:
        # 客服账号被软删除/换号时不能静默失效，退到管理员并留日志
        fallback = [str(x).strip() for x in (getattr(cfg, 'ADMIN_TELEGRAM_IDS', []) or [])]
        targets = [x for x in fallback if x]
        if targets:
            logger.warning('新订单通知：客服用户名没解析到 chat_id，已退到管理员 %s', targets)
    return targets


def send_new_order_notifications(text: str, target_ids=None):
    """逐个目标发送，单个失败不影响其他目标，也绝不影响已落库的订单。"""
    results = []
    targets = target_ids or []
    for chat_id in targets:
        try:
            ok, detail = send_telegram_message(str(chat_id), text)
        except Exception as exc:
            ok, detail = False, str(exc)
        results.append({"chat_id": str(chat_id), "sent": bool(ok), "detail": detail})
        if not ok:
            logger.warning('新订单通知发送失败 chat_id=%s: %s', chat_id, detail)
    return results


def resolve_split_order_targets(session: Session) -> List[str]:
    """分单强提醒目标：专用群 → 新订单通知目标 → 管理员。"""
    import config as cfg
    targets = [
        str(value).strip()
        for value in (getattr(cfg, 'SPLIT_ORDER_GROUP_IDS', []) or [])
        if str(value).strip()
    ]
    if not targets:
        # 分单强提醒不受普通新订单通知开关影响；否则 .24 关闭
        # ORDER_NOTIFY_ENABLED 时，已配置的分单回退目标也会静默失效。
        targets = [
            str(value).strip()
            for value in (getattr(cfg, 'ORDER_NOTIFICATION_TELEGRAM_IDS', []) or [])
            if str(value).strip()
        ]
    if not targets:
        # 群录单白名单就是实际运营群；没有单独配置分单群时，优先把
        # 分单强提醒发回这个群，而不是静默回退到空的管理员列表。
        targets = [
            str(value).strip()
            for value in (getattr(cfg, 'AGENT_GROUP_IDS', []) or [])
            if str(value).strip()
        ]
    if not targets and getattr(cfg, 'ORDER_NOTIFY_ENABLED', False):
        targets = resolve_order_notification_targets(session)
    if not targets:
        targets = [
            str(value).strip()
            for value in (getattr(cfg, 'ADMIN_TELEGRAM_IDS', []) or [])
            if str(value).strip()
        ]
    return list(dict.fromkeys(targets))


def build_split_order_alert(orders: List[Order]) -> str:
    """构建醒目的正式分单群提醒。"""
    if not orders:
        return ''
    first = orders[0]
    grouped = _group_order_items([
        item for order in orders for item in (getattr(order, 'items', None) or [])
    ])
    total_units = __import__('product_units').order_total_label([
        item for order in orders for item in (getattr(order,'items',None) or [])])[1:]
    total_amount = sum(float(getattr(order, 'grand_total', 0) or 0) for order in orders)
    item_text = '\n'.join(html.escape(group['text']) for group in grouped) or '-'
    numbers = '、'.join(f"#{html.escape(str(order.order_number))}" for order in orders)
    return (
        '🚨🚨 <b>分单订单 · 请优先确认</b> 🚨🚨\n\n'
        f'<b>拆单组：</b>{html.escape(str(first.split_group_id or first.order_number))}\n'
        f'<b>子订单：</b>{numbers}\n'
        f'<b>共计：</b>{len(orders)} 单 / {total_units} / ¥{total_amount:.2f}\n'
        f'<b>收件人：</b>{html.escape(str(first.recipient_name or ""))} '
        f'{html.escape(str(first.recipient_phone or ""))}\n'
        f'<b>地址：</b>{html.escape(str(first.recipient_address or ""))}\n\n'
        f'<b>商品：</b>\n{item_text}\n\n'
        '⚠️ 当前仅显示在“分单订单”，确认后才进入后续录单流程。'
    )


def build_confirm_notify_text(order: Order) -> str:
    return (
        '订单已确认' + chr(10) + chr(10) +
        f'订单号: #{order.order_number}' + chr(10) +
        f'应付总额: {float(order.grand_total or 0):.2f}' + chr(10) +
        '状态: 已确认，等待发货' + chr(10) +
        f'收件人: {order.recipient_name} / {order.recipient_phone}'
    )

def get_db_session():
    """获取数据库会话"""
    yield from db.get_db()


from telegram_accounts_admin import make_router as make_accounts_router
app.include_router(make_accounts_router(get_db_session,require_admin_or_api_secret))

@app.get('/api/bundles/catalog')
def bundle_catalog_api(session: Session=Depends(get_db_session),_auth: str=Depends(require_admin_or_api_secret)):
    if not inspect_database(db.engine).has_table('bundle_definitions'):
        return []
    return __import__('bundles').public_catalog(session)

@app.post('/api/bundles/preview')
def bundle_preview_api(telegram_id:str=Form(...),bundle_requests:str=Form(...),items:str=Form('[]'),
                       session: Session=Depends(get_db_session),_auth:str=Depends(require_admin_or_api_secret)):
    if not inspect_database(db.engine).has_table('bundle_definitions'):
        raise HTTPException(503,'福袋数据库升级尚未完成')
    user=session.query(User).filter_by(telegram_id=telegram_id).first()
    import telegram_accounts,json
    if not user or not telegram_accounts.eligible(session,user):
        raise HTTPException(403,'当前暂无下单权限')
    try:
        requests=json.loads(bundle_requests);ordinary=json.loads(items)
        if not isinstance(requests,list) or not requests or not isinstance(ordinary,list):
            raise ValueError('福袋请求格式无效')
        rows=[]
        for item in ordinary:
            product=_resolve_order_product(session,item,user=user)
            rows.append(dict(product_id=product.id,quantity=__import__('bundles').positive(item.get('quantity'),'商品数量'),warehouse_id=product.default_warehouse_id))
        return __import__('bundles').preview(db,session,user,requests,rows,cross_warehouse_confirmed=True)
    except (ValueError,TypeError,KeyError) as error:raise HTTPException(400,str(error)) from None


def verify_api_key(api_key: str = Query(..., alias="api_key")):
    """验证 API 密钥（Query 方式，兼容旧调用）"""
    if api_key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="无效的API密钥")
    return api_key


def require_api_secret(
    request: Request,
    api_key: Optional[str] = Query(None, alias="api_key"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """写操作鉴权：支持 Header X-API-Key / Query api_key。"""
    header_key = x_api_key or request.headers.get('X-API-Key') or request.headers.get('x-api-key')
    key = header_key or api_key
    # 兼容 form 中的 api_key（部分前端只放 form）
    if not key:
        try:
            # starlette form is awaitable; here only fallback via query already handled
            pass
        except Exception:
            pass
    if not key or key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="无效的API密钥（需要 X-API-Key 或 api_key）")
    return key


# ==================== 后台登录 ====================
@app.get("/login", response_class=HTMLResponse)
@_run_blocking_endpoint
def login_page(request: Request, next: str = "/admin"):
    if is_admin_logged_in(request):
        return RedirectResponse(url=next or "/admin", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "next": next or "/admin", "username": ""},
    )


@app.post("/login", response_class=HTMLResponse)
@_run_blocking_endpoint
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        request.session.clear()
        request.session["admin_logged_in"] = True
        request.session["admin_username"] = username
        target = next if isinstance(next, str) and next.startswith("/") else "/admin"
        return RedirectResponse(url=target, status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "账号或密码错误",
            "next": next or "/admin",
            "username": username,
        },
        status_code=401,
    )


@app.get("/logout")
@_run_blocking_endpoint
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/api/auth/me")
@_run_blocking_endpoint
def auth_me(request: Request):
    if not is_admin_logged_in(request):
        raise HTTPException(status_code=401, detail="未登录")
    return {
        "logged_in": True,
        "username": request.session.get("admin_username") or ADMIN_USERNAME,
    }


# 首页
@app.get("/", response_class=HTMLResponse)
@_run_blocking_endpoint
def index(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """管理后台首页"""
    return templates.TemplateResponse(request, "index.html", {"title": "彩虹仓"})


# ==================== API 接口 ====================

@app.post('/api/users/admin-create')
@_run_blocking_endpoint
def admin_create_user(request:Request,telegram_id:str=Form(...),authorize:bool=Form(True),
                      session:Session=Depends(get_db_session)):
    if require_admin_session(request) is not None:raise HTTPException(401,'请先登录后台')
    from admin_user_onboarding import profile,create
    import re
    telegram_id=telegram_id.strip()
    if not re.fullmatch(r'[1-9][0-9]{4,18}',telegram_id):raise HTTPException(400,'请输入有效的数字 Telegram 用户ID')
    diagnostic={}
    identity=profile(telegram_id,TELEGRAM_BOT_TOKEN,diagnostic=diagnostic)
    try:
        user,created=create(session,telegram_id,identity,authorize)
        # Existing accounts: update verified profile only, never authorization/prices.
        if identity:
            user.telegram_name=identity['telegram_name'];user.username=identity['username']
        session.commit()
    except ValueError as error:
        session.rollback();raise HTTPException(400,str(error))
    from telegram_accounts import eligible
    return dict(id=user.id,telegram_id=user.telegram_id,created=created,authorized=eligible(session,user),
                profile_verified=bool(identity),profile_diagnostic=diagnostic,
                message='用户已添加' if created else '用户已存在，原权限未改动')

@app.post('/api/users/{user_id:int}/refresh-profile')
@_run_blocking_endpoint
def refresh_user_profile(user_id:int,request:Request,session:Session=Depends(get_db_session)):
    if require_admin_session(request) is not None:raise HTTPException(401,'请先登录后台')
    user=session.get(User,user_id)
    if not user:raise HTTPException(404,'用户不存在')
    from admin_user_onboarding import profile
    diagnostic={}
    identity=profile(user.telegram_id,TELEGRAM_BOT_TOKEN,diagnostic=diagnostic)
    if not identity:raise HTTPException(409,diagnostic)
    user.telegram_name=identity['telegram_name'];user.username=identity['username'];session.commit()
    return dict(success=True,telegram_id=user.telegram_id,**identity)

@app.post("/api/users/create_or_get")
@_run_blocking_endpoint
def create_or_get_user(
    telegram_id: str = Form(...),
    username: Optional[str] = Form(None),
    telegram_name: Optional[str] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """创建或获取用户"""
    user = session.query(User).filter(User.telegram_id == telegram_id).first()

    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            telegram_name=(telegram_name or "").strip() or None,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    else:
        changed = False
        if telegram_name and user.telegram_name != telegram_name.strip():
            user.telegram_name = telegram_name.strip()
            changed = True
        if username and user.username != username.strip():
            user.username = username.strip()
            changed = True
        if changed:
            session.commit()

    debt = db.calc_user_debt(user.id, session=session)
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "telegram_name": getattr(user, 'telegram_name', None),
        "display_name": getattr(user, 'display_name', None),
        "balance": user.balance,
        "debt": debt,
        "preferred_pay_method": getattr(user, 'preferred_pay_method', 'cny'),
        "agent_level": user.agent_level,
        "agent_level_name": user.get_agent_level_name(),
    }


@app.get("/api/users/{user_id:int}")
@_run_blocking_endpoint
def get_user(
    user_id: int,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """获取用户信息"""
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "telegram_name": getattr(user, 'telegram_name', None),
        "display_name": getattr(user, 'display_name', None),
        "balance": user.balance,
        "debt": db.calc_user_debt(user.id, session=session),
        "preferred_pay_method": getattr(user, 'preferred_pay_method', 'cny'),
        "agent_level": user.agent_level,
        "agent_level_name": user.get_agent_level_name(),
        "created_at": user.created_at,
        "is_active": user.is_active,
        "remark": getattr(user, 'remark', None),
        "prices": db.get_user_product_prices(user.id),
    }


def _user_order_summary_subquery(session: Session):
    """为后台用户相关列表复用订单数量和欠款聚合。"""
    outstanding = case(
        (
            and_(
                Order.status != "cancelled",
                Order.grand_total > Order.paid_amount,
            ),
            Order.grand_total - Order.paid_amount,
        ),
        else_=0.0,
    )
    return (
        session.query(
            Order.user_id.label("user_id"),
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(outstanding), 0.0).label("debt"),
        )
        .group_by(Order.user_id)
        .subquery()
    )


def _users_listing(session, *, authorized_only=True, include_inactive=False, include_authorization=False):
    """Shared serializer; financial audit is separate from formal users."""
    order_summary = _user_order_summary_subquery(session)
    query = session.query(
        User,
        func.coalesce(order_summary.c.order_count, 0).label("order_count"),
        func.coalesce(order_summary.c.debt, 0.0).label("debt"),
    ).outerjoin(order_summary, order_summary.c.user_id == User.id)
    if authorized_only:
        import telegram_accounts
        query=query.filter(telegram_accounts.eligibility_filter())
    elif not include_inactive:
        query = query.filter(User.is_active == True)
    users = query.order_by(User.created_at.desc()).all()
    custom_price_counts=dict(session.query(UserProductPrice.user_id,func.count(UserProductPrice.id)).group_by(UserProductPrice.user_id).all())
    for uid,count in session.query(UserCategoryPrice.user_id,func.count(UserCategoryPrice.id)).group_by(UserCategoryPrice.user_id):
        custom_price_counts[uid]=custom_price_counts.get(uid,0)+count
    result = []
    authorizations={}
    if include_authorization:
        for row in session.query(DropshipAuthRequest).order_by(DropshipAuthRequest.created_at.desc(),DropshipAuthRequest.id.desc()):
            authorizations.setdefault(row.user_id,row.status)
    for user, order_count, debt in users:
        from manual_receivables import outstanding as manual_outstanding
        debt=float(debt or 0)+manual_outstanding(session,user.id)
        result.append({
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "telegram_name": getattr(user, 'telegram_name', None),
            "display_name": getattr(user, 'display_name', None),
            "balance": user.balance,
            "debt": round(float(debt or 0.0), 2),
            "preferred_pay_method": getattr(user, 'preferred_pay_method', 'cny'),
            "agent_level": user.agent_level,
            "agent_level_name": user.get_agent_level_name(),
            "created_at": user.created_at,
            "is_active": user.is_active,
            "order_count": int(order_count or 0),
            "custom_price_count": custom_price_counts.get(user.id,0),
            "remark": getattr(user, 'remark', None),
            **({'authorization_status':authorizations.get(user.id,'none')} if include_authorization else {}),
        })
    return result


@app.get('/api/users/management')
@_run_blocking_endpoint
def get_managed_users(request:Request,session:Session=Depends(get_db_session)):
    if require_admin_session(request) is not None:
        raise HTTPException(401,'请先登录后台')
    return JSONResponse(jsonable_encoder(_users_listing(session,authorized_only=False,
        include_authorization=True)),headers={'Cache-Control':'no-store'})


@app.get("/api/users")
@_run_blocking_endpoint
def get_users(
    include_inactive: bool = False,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    # Inactive/nonapproved accounts never enter the formal list, including
    # when a client supplies the legacy include_inactive parameter.
    return _users_listing(session,authorized_only=True)


@app.get("/api/users/audit")
@_run_blocking_endpoint
def get_users_audit(
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """Historical finance visibility only; this does not grant order rights."""
    return _users_listing(session,authorized_only=False,include_inactive=True)


@app.post("/api/users/{user_id}/delete")
@_run_blocking_endpoint
def delete_user(
    user_id: int,
    reason: Optional[str] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """软删除用户：禁用账号并释放原 Telegram ID，保留历史订单/收款记录。"""
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not user.is_active:
        return {"success": True, "message": "用户已是删除/禁用状态", "user_id": user.id}

    original_telegram_id = user.telegram_id
    deleted_suffix = uuid.uuid4().hex[:6]
    user.telegram_id = f"deleted:{user.id}:{deleted_suffix}:{original_telegram_id}"
    if user.username and not str(user.username).startswith("deleted_"):
        user.username = f"deleted_{user.username}"
    user.is_active = False
    old_remark = (getattr(user, "remark", None) or "").strip()
    delete_note = f"[删除用户] 原Telegram ID: {original_telegram_id}"
    if reason:
        delete_note += f"；原因: {reason.strip()}"
    user.remark = (old_remark + "\n" + delete_note).strip() if old_remark else delete_note
    session.commit()
    return {
        "success": True,
        "message": "用户已删除，历史订单和收款记录已保留",
        "user_id": user.id,
        "original_telegram_id": original_telegram_id,
    }


def _serialize_dropship_request(
    row: DropshipAuthRequest,
    user: User = None,
    order_count: int = None,
    debt: float = None,
):
    user = user if user is not None else row.user
    return {
        "id": row.id,
        "user_id": row.user_id,
        "telegram_id": row.telegram_id,
        "username": row.username,
        "telegram_name": getattr(user, "telegram_name", None) if user else None,
        "status": row.status,
        "message": row.message,
        "admin_note": row.admin_note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "user_display_name": getattr(user, "display_name", None) if user else None,
        "user_remark": getattr(user, "remark", None) if user else None,
        "agent_level": getattr(user, "agent_level", 0) if user else 0,
        "agent_level_name": user.get_agent_level_name() if user else "",
        "order_count": (
            int(order_count or 0)
            if order_count is not None
            else (len(user.orders) if user else 0)
        ),
        "debt": (
            round(float(debt or 0.0), 2)
            if debt is not None
            else (db.calc_user_debt(row.user_id) if row.user_id else 0)
        ),
    }


@app.post("/api/dropship/apply")
@_run_blocking_endpoint
def apply_dropship_auth(
    telegram_id: str = Form(...),
    username: Optional[str] = Form(None),
    telegram_name: Optional[str] = Form(None),
    message: Optional[str] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """用户申请代发下单授权。"""
    telegram_id = str(telegram_id).strip()
    username = (username or "").strip() or None
    telegram_name = (telegram_name or "").strip() or None
    message = (message or "").strip() or None
    if not telegram_id:
        raise HTTPException(status_code=400, detail="Telegram ID 不能为空")

    user = session.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            telegram_name=telegram_name,
        )
        session.add(user)
        session.flush()
    else:
        changed = False
        if username and user.username != username:
            user.username = username
            changed = True
        if telegram_name and user.telegram_name != telegram_name:
            user.telegram_name = telegram_name
            changed = True
        if changed:
            session.flush()

    latest = (
        session.query(DropshipAuthRequest)
        .filter(DropshipAuthRequest.user_id == user.id)
        .order_by(DropshipAuthRequest.created_at.desc(), DropshipAuthRequest.id.desc())
        .first()
    )
    if latest and latest.status == "approved":
        session.commit()
        session.refresh(latest)
        return {
            "success": True,
            "already_approved": True,
            "message": "已通过代发授权，可联系管理员确认单价",
            "request": _serialize_dropship_request(latest),
        }
    if latest and latest.status == "pending":
        latest.telegram_id = telegram_id
        latest.username = username
        if message:
            latest.message = message
        latest.updated_at = datetime.utcnow()
        row = latest
    else:
        row = DropshipAuthRequest(
            user_id=user.id,
            telegram_id=telegram_id,
            username=username,
            status="pending",
            message=message,
        )
        session.add(row)

    session.commit()
    session.refresh(row)
    return {
        "success": True,
        "already_approved": False,
        "message": "申请已提交，等待管理员设置单价",
        "request": _serialize_dropship_request(row),
    }


@app.get("/api/dropship/status")
@_run_blocking_endpoint
def get_dropship_auth_status(
    telegram_id: str = Query(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """Return the latest authorization state used by group auto-ordering."""
    telegram_id = str(telegram_id or '').strip()
    if not telegram_id:
        raise HTTPException(status_code=400, detail="Telegram ID 不能为空")
    user = session.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return {
            "authorized": False,
            "status": "none",
            "message": "请先申请代发下单权限",
        }
    row = (
        session.query(DropshipAuthRequest)
        .filter(DropshipAuthRequest.user_id == user.id)
        .order_by(DropshipAuthRequest.created_at.desc(), DropshipAuthRequest.id.desc())
        .first()
    )
    status = row.status if row else "none"
    messages = {
        "none": "请先申请代发下单权限",
        "pending": "代发授权申请正在等待后台审核",
        "approved": "代发下单权限已通过",
        "rejected": "代发授权未通过，订单不会进入识别流程",
    }
    return {
        "authorized": status == "approved" and bool(user.is_active),
        "status": status,
        "message": messages.get(status, "请先申请代发下单权限"),
    }


@app.get("/api/dropship/applications")
@_run_blocking_endpoint
def list_dropship_applications(
    status: str = Query("pending"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """后台查看代发授权申请。"""
    order_summary = _user_order_summary_subquery(session)
    query = (
        session.query(
            DropshipAuthRequest,
            User,
            func.coalesce(order_summary.c.order_count, 0).label("order_count"),
            func.coalesce(order_summary.c.debt, 0.0).label("debt"),
        )
        .outerjoin(User, User.id == DropshipAuthRequest.user_id)
        .outerjoin(order_summary, order_summary.c.user_id == DropshipAuthRequest.user_id)
    )
    if status and status != "all":
        query = query.filter(DropshipAuthRequest.status == status)
    rows = (
        query.order_by(DropshipAuthRequest.created_at.desc(), DropshipAuthRequest.id.desc())
        .limit(limit)
        .all()
    )
    return [
        _serialize_dropship_request(
            row,
            user=user,
            order_count=order_count,
            debt=debt,
        )
        for row, user, order_count, debt in rows
    ]


@app.post("/api/dropship/applications/{request_id}/status")
@_run_blocking_endpoint
def update_dropship_application_status(
    request_id: int,
    status: str = Form(...),
    admin_note: Optional[str] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """后台更新代发授权申请状态。"""
    status = (status or "").strip()
    if status not in {"pending", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail="状态无效")
    row = session.query(DropshipAuthRequest).filter(DropshipAuthRequest.id == request_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="申请不存在")
    row.status = status
    if admin_note is not None:
        row.admin_note = admin_note.strip() or None
    row.updated_at = datetime.utcnow()
    session.commit()
    session.refresh(row)
    return {
        "success": True,
        "message": "申请状态已更新",
        "request": _serialize_dropship_request(row),
    }


@app.post("/api/users/{user_id}/update_agent_level")
@_run_blocking_endpoint
def update_user_agent_level(
    user_id: int,
    agent_level: int = Form(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """更新用户客户等级"""
    # 验证客户等级
    if agent_level not in [0, 1, 2]:
        raise HTTPException(
            status_code=400,
            detail="客户等级无效：0=VIP客户, 1=超级VIP, 2=合伙人"
        )

    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    old_level = user.agent_level
    user.agent_level = agent_level
    session.commit()

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "agent_level": user.agent_level,
        "agent_level_name": user.get_agent_level_name(),
        "message": f"客户等级已从 {old_level} 更新为 {agent_level}"
    }


def _next_product_serial(session: Session, is_parent: bool) -> int:
    column = Product.category_serial if is_parent else Product.product_serial
    rows = session.query(column).filter(Product.is_parent == is_parent, column.isnot(None)).all()
    serials = [int(row[0]) for row in rows if row[0]]
    return (max(serials) + 1) if serials else 1


def _display_serial(prefix: str, serial: Optional[int]) -> str:
    return f"{prefix}{int(serial or 0):03d}" if serial else ""


def _batch_product_items(payload):
    import catalog_product_batch as batch
    return batch.parse_text(payload.text,payload.parent_id,payload.warehouse_id) if payload.text is not None else [item.model_dump() for item in payload.items]


@app.post('/api/products/batch_preview')
@_run_blocking_endpoint
def preview_batch_products(payload: BatchProductRequest, session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret)):
    import catalog_product_batch as batch
    try:return batch.plan(session,_batch_product_items(payload))
    except ValueError as exc:raise HTTPException(400,str(exc))


@app.post("/api/products/batch_create")
@_run_blocking_endpoint
def batch_create_products(
    payload: BatchProductRequest,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """整批创建/恢复口味及初始入库，使用既有进货价快照链路。"""
    import catalog_product_batch as batch
    try:
        if payload.text is not None and not payload.preview_token:
            raise ValueError('请先预览校验，再确认整批执行')
        return batch.execute(db,session,_batch_product_items(payload),_next_product_serial,payload.preview_token)
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/products/create")
@_run_blocking_endpoint
def create_product(
    name: str = Form(...),
    price_review_token: str = Form(''),
    cost_price: Optional[float] = Form(None),  # 空值与明确0元区分
    reference_price_state: Optional[str] = Form(None),
    level1_agent_price: str = Form('0'),  # 合伙人价：保留原始金额文本
    level2_agent_price: str = Form('0'),  # 超级VIP价
    regular_price: str = Form(...),  # VIP客户价
    description: Optional[str] = Form(None),
    initial_stock: int = Form(0),
    warehouse_id: Optional[int] = Form(None),  # 绑定仓库
    parent_id: Optional[int] = Form(None),  # 父级分类ID
    is_parent: bool = Form(False),  # 是否为父级分类
    low_stock_threshold: int = Form(20),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """创建商品（支持多级价格、绑定仓库和父级分类）"""
    level1_agent_price,level2_agent_price,regular_price = map(_sale_form_amount,
        (level1_agent_price,level2_agent_price,regular_price))
    from catalog_retirement import require_operating
    require_operating(session, 'warehouse', warehouse_id)
    require_operating(session, 'product', parent_id)
    name = name.strip()
    if not name:
        raise HTTPException(400, '名称不能为空')
    # 分类唯一性以当前仓库和生命周期为准；归档对象可安全复用。
    if is_parent:
        if session.bind.dialect.name == 'sqlite':
            connection = session.connection()
            if not connection.connection.driver_connection.in_transaction:
                session.execute(sql_text('BEGIN IMMEDIATE'))

        # 分类可以绑一个仓库；下面新增口味没单独选仓库时会继承它
        warehouse = None
        if warehouse_id:
            warehouse = session.query(Warehouse).filter(
                Warehouse.id == warehouse_id,
                Warehouse.is_active == True,
            ).first()
        else:
            active_warehouses = session.query(Warehouse).filter(
                Warehouse.is_active == True,
            ).all()
            if len(active_warehouses) == 1:
                warehouse = active_warehouses[0]
        if not warehouse:
            raise HTTPException(status_code=400, detail="创建分类时必须选择有效仓库")
        from category_lifecycle import restore_or_create
        try:
            parent = restore_or_create(session, name, warehouse.id)
        except ValueError as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from None
        restored = parent is not None
        if parent is None:
            parent = Product(
                name=name,
                is_parent=True,
                regular_price=0,
                default_warehouse_id=warehouse.id,
                low_stock_threshold=normalize_low_stock_threshold(low_stock_threshold),
                category_serial=_next_product_serial(session, True),
            )
            session.add(parent)
        session.commit()
        session.refresh(parent)

        return {
            "id": parent.id,
            "category_serial": parent.category_serial,
            "category_display_id": _display_serial("C", parent.category_serial),
            "name": parent.name,
            "is_parent": True,
            "restored": restored,
            "default_warehouse_id": parent.default_warehouse_id,
            "default_warehouse_name": warehouse.name,
            "created_at": parent.created_at
        }

    if not isinstance(reference_price_state,(str,type(None))): reference_price_state=None
    reference_price_state = reference_price_state or ('UNSET' if cost_price is None else 'SET')
    if reference_price_state not in ('SET','UNSET'):
        raise HTTPException(400,'新商品必须明确参考成本状态')
    if reference_price_state == 'UNSET': cost_price = 0.0
    if cost_price is None or not __import__('math').isfinite(cost_price) or cost_price < 0:
        raise HTTPException(400,'参考成本必须为有限非负数')
    # 成本不限制售价：清仓允许亏损；客户等级顺序暂沿用原规则。
    if not all(__import__('math').isfinite(v) and v >= 0 for v in (level1_agent_price, level2_agent_price, regular_price)):
        raise HTTPException(400, '售价必须为有限的非负数')
    if not (level1_agent_price <= level2_agent_price <= regular_price):
        raise HTTPException(
            status_code=400,
            detail="价格层级不正确：合伙人价 <= 超级VIP价 <= VIP客户价（售价允许低于成本）"
        )

    # Serialize catalog identity reuse with batch creation; no new schema/index.
    if session.bind.dialect.name=='sqlite':
        # A caller may already own a write transaction (e.g. catalog import).
        # Never commit or restart that transaction merely to acquire the lock.
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            session.execute(sql_text('BEGIN IMMEDIATE'))
    # 验证父级分类
    parent = None
    requested_parent_id = parent_id
    if parent_id:
        parent = session.query(Product).filter(
            Product.id == parent_id,
            Product.is_parent == True
        ).first()
        if not parent:
            raise HTTPException(status_code=400, detail="指定的父级分类不存在")
        if parent.is_archived or not parent.is_active:
            raise HTTPException(400,'指定分类已删除或停用，请重新选择分类')
    else:
        # 如果没有指定父级，使用或创建默认父级
        parent = None

    existing = session.query(Product).filter(
        Product.name == name,
        Product.parent_id == parent_id,
        Product.is_parent == False,
    ).first()
    if existing and not existing.is_archived and requested_parent_id is not None:
        raise HTTPException(status_code=400, detail="该型号下已存在同名口味")

    # 验证仓库
    warehouse = None
    if warehouse_id:
        warehouse = session.query(Warehouse).filter(Warehouse.id == warehouse_id,Warehouse.is_active == True).first()
        if not warehouse:
            raise HTTPException(status_code=400, detail="指定的仓库不存在")
    else:
        # 没单独选仓库：先继承所属分类绑定的仓库，再退回第一个启用仓库
        inherited_warehouse_id = getattr(parent, "default_warehouse_id", None)
        warehouse = session.query(Warehouse).filter(
            Warehouse.id == inherited_warehouse_id,
            Warehouse.is_active == True,
        ).first() if inherited_warehouse_id else None
        if not warehouse:
            warehouse = session.query(Warehouse).filter(Warehouse.is_active == True).first()
        if not warehouse:
            raise HTTPException(status_code=400, detail="系统未配置仓库，请先创建仓库")

    if parent is None:
        parent = session.query(Product).filter(
            Product.name == "默认分类",
            Product.is_parent == True,
            Product.default_warehouse_id == warehouse.id,
        ).first()
        if parent and parent.is_archived:
            raise HTTPException(400,'默认分类已删除，请明确选择有效分类，系统不会自动重建')
        if not parent:
            parent = Product(
                name="默认分类",
                is_parent=True,
                regular_price=0,
                default_warehouse_id=warehouse.id,
                low_stock_threshold=20,
                category_serial=_next_product_serial(session, True),
            )
            session.add(parent)
            session.flush()
        parent_id = parent.id
    if parent.default_warehouse_id != warehouse.id:
        raise HTTPException(status_code=400, detail="商品和父级分类必须属于同一仓库")

    existing = session.query(Product).filter(
        Product.name == name,
        Product.parent_id == parent_id,
        Product.is_parent == False,
    ).first()
    import catalog_product_batch as catalog_batch
    try:
        existing, action = catalog_batch.reusable(session,parent_id,name)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    if action=='existing':
        raise HTTPException(status_code=400, detail="该型号下已存在同名口味")

    from types import SimpleNamespace
    review_product=existing if existing else SimpleNamespace(id=0,default_warehouse_id=warehouse.id)
    review_prices=({k:getattr(existing,k) for k in ('level1_agent_price','level2_agent_price','regular_price')}
        if action=='restore' else dict(level1_agent_price=level1_agent_price,level2_agent_price=level2_agent_price,regular_price=regular_price))
    __import__('sale_price_review').catalog_confirmation(session,[review_product],
        review_prices,
        price_review_token,dict(route='catalog_create',name=name,parent_id=parent_id,warehouse_id=warehouse.id))
    product = existing if action=='restore' else Product(
        name=name,
        cost_price=cost_price,
        reference_price_state=reference_price_state,
        level1_agent_price=level1_agent_price,
        level2_agent_price=level2_agent_price,
        regular_price=regular_price,
        description=description,
        default_warehouse_id=warehouse.id,
        parent_id=parent_id,
        low_stock_threshold=normalize_low_stock_threshold(low_stock_threshold),
        product_serial=_next_product_serial(session, False),
    )
    if initial_stock<0:raise HTTPException(400,'初始库存不能小于0')
    if action=='restore':
        require_operating(session, 'product', product.id)
        if product.default_warehouse_id!=warehouse.id:raise HTTPException(400,'归档商品仓库与分类不一致，不能自动恢复')
        product.is_archived=False;product.is_active=True;product.auto_unlisted=False
    try:
        session.add(product)
        session.flush()
        catalog_batch.initial_stock(db,session,product,initial_stock)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(400,str(exc))
    from inventory_preview import invalidate_warehouse_cache
    invalidate_warehouse_cache(warehouse.id)
    session.refresh(product)

    # 获取父级名称
    parent_name = None
    if product.parent:
        parent_name = product.parent.name

    return {
        "id": product.id,
        "product_serial": product.product_serial,
        "product_display_id": _display_serial("P", product.product_serial),
        "name": product.name,
        "cost_price": product.cost_price,
        "level1_agent_price": product.level1_agent_price,
        "level2_agent_price": product.level2_agent_price,
        "regular_price": product.regular_price,
        "description": product.description,
        "default_warehouse_id": product.default_warehouse_id,
        "default_warehouse_name": warehouse.name,
        "parent_id": product.parent_id,
        "parent_name": parent_name,
        "low_stock_threshold": product.low_stock_threshold,
        "stock": initial_stock,
        "auto_unlisted": product.auto_unlisted,
        "is_active": product.is_active,
        "created_at": product.created_at
    }


@app.post("/api/products/{product_id}/update")
@_run_blocking_endpoint
def update_product(
    product_id: int,
    price_review_token: str = Form(''),
    name: Optional[str] = Form(None),
    cost_price: Optional[float] = Form(None),
    reference_price_state: Optional[str] = Form(None),
    level1_agent_price: Optional[str] = Form(None),
    level2_agent_price: Optional[str] = Form(None),
    regular_price: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    is_active: Optional[bool] = Form(None),
    warehouse_id: Optional[int] = Form(None),  # 更新绑定仓库
    parent_id: Optional[int] = Form(None),
    low_stock_threshold: Optional[int] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """更新商品信息（支持多级价格和更换仓库）"""
    level1_agent_price,level2_agent_price,regular_price = map(_sale_form_amount,
        (level1_agent_price,level2_agent_price,regular_price))
    from catalog_retirement import require_operating
    require_operating(session, 'product', product_id)
    require_operating(session, 'product', parent_id)
    require_operating(session, 'warehouse', warehouse_id)
    # Serialize stock validation and catalog movement with inventory writers.
    # No historical stock/cost rows are reassigned by this transaction.
    if session.get_bind().dialect.name == 'sqlite' and not session.in_transaction():
        session.execute(sql_text('BEGIN IMMEDIATE'))
    product = session.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    __import__('sale_price_review').catalog_confirmation(session,[product],
        dict(level1_agent_price=level1_agent_price,level2_agent_price=level2_agent_price,regular_price=regular_price),
        price_review_token,dict(route='catalog_update',product_id=product_id))
    target_name = name.strip() if name else product.name
    if name is not None:
        try:__import__('catalog_rename').record_rename(session,product,name,_auth)
        except ValueError as error:raise HTTPException(400,str(error)) from None
    target_parent_id = parent_id if parent_id is not None else product.parent_id
    target_warehouse_id = warehouse_id if warehouse_id is not None else product.default_warehouse_id
    target_parent = None
    if target_parent_id is not None:
        target_parent = session.query(Product).filter(
            Product.id == target_parent_id,
            Product.is_parent == True,
            Product.is_archived == False,
        ).first()
        if parent_id is not None and not target_parent:
            raise HTTPException(status_code=400, detail="指定的父级分类不存在")
        if target_parent and target_parent.default_warehouse_id != target_warehouse_id:
            raise HTTPException(status_code=400, detail="商品和父级分类必须属于同一仓库")
    if product.is_parent:
        from category_warehouse import apply_move
        try:
            apply_move(session,product.id,target_warehouse_id,target_name)
        except ValueError as error:
            raise HTTPException(400,str(error)) from None
    if parent_id is not None:
        parent = target_parent
    if target_name and not product.is_parent:
        exists = session.query(Product).filter(
            Product.id != product_id,
            Product.name == target_name,
            Product.parent_id == target_parent_id,
            Product.is_parent == False,
        ).first()
        if exists:
            raise HTTPException(status_code=400, detail="该型号下已存在同名口味")
        product.name = target_name
    elif target_name:
        product.name = target_name
    if parent_id is not None:
        product.parent_id = parent_id
    if reference_price_state is not None and reference_price_state not in ('SET','UNSET'):
        raise HTTPException(400,'参考成本状态无效')
    if reference_price_state == 'UNSET':
        cost_price=0.0
        product.reference_price_state='UNSET'
    elif reference_price_state == 'SET' and cost_price is None:
        raise HTTPException(400,'已填写状态需要明确参考成本')
    if cost_price is not None:
        if not __import__('math').isfinite(cost_price) or cost_price < 0:
            raise HTTPException(400,'参考成本必须为有限非负数')
        product.cost_price = cost_price
        product.reference_price_state=reference_price_state or 'SET'
    if level1_agent_price is not None:
        if not __import__('math').isfinite(level1_agent_price) or level1_agent_price < 0:
            raise HTTPException(400, '售价必须为有限的非负数')
        product.level1_agent_price = level1_agent_price
    if level2_agent_price is not None:
        if not __import__('math').isfinite(level2_agent_price) or level2_agent_price < 0:
            raise HTTPException(400, '售价必须为有限的非负数')
        product.level2_agent_price = level2_agent_price
    if regular_price is not None:
        if not __import__('math').isfinite(regular_price) or regular_price < 0:
            raise HTTPException(400, '售价必须为有限的非负数')
        product.regular_price = regular_price
    if description is not None:
        product.description = description
    if is_active is not None:
        product.is_active = is_active
        product.auto_unlisted = False
    if low_stock_threshold is not None:
        product.low_stock_threshold = normalize_low_stock_threshold(low_stock_threshold, product.low_stock_threshold)

    # 验证价格层级（如果所有价格都提供了）
    all_prices_provided = all(v is not None for v in [cost_price, level1_agent_price, level2_agent_price, regular_price])
    if all_prices_provided:
        if not (product.level1_agent_price <= product.level2_agent_price <= product.regular_price):
            raise HTTPException(
                status_code=400,
                detail="价格层级不正确：合伙人价 <= 超级VIP价 <= VIP客户价（售价允许低于成本）"
            )

    # 更新绑定仓库
    if warehouse_id is not None:
        warehouse = session.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
        if not warehouse:
            raise HTTPException(status_code=400, detail="指定的仓库不存在")
        product.default_warehouse_id = warehouse.id

    db.refresh_product_stock_status(session, product.id)
    session.commit()
    session.refresh(product)

    # 获取仓库名称
    warehouse_name = None
    if product.default_warehouse_id:
        warehouse = session.query(Warehouse).filter(Warehouse.id == product.default_warehouse_id).first()
        if warehouse:
            warehouse_name = warehouse.name

    return {
        "id": product.id,
        "product_serial": product.product_serial,
        "product_display_id": _display_serial("P", product.product_serial),
        "name": product.name,
        "cost_price": product.cost_price,
        "level1_agent_price": product.level1_agent_price,
        "level2_agent_price": product.level2_agent_price,
        "regular_price": product.regular_price,
        "description": product.description,
        "is_active": product.is_active,
        "parent_id": product.parent_id,
        "low_stock_threshold": product.low_stock_threshold,
        "auto_unlisted": product.auto_unlisted,
        "default_warehouse_id": product.default_warehouse_id,
        "default_warehouse_name": warehouse_name
    }


@app.get("/api/products")
@_run_blocking_endpoint
def get_products(
    include_inactive: bool = False,
    include_parents: bool = False,
    warehouse_id: Optional[int] = None,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """获取所有商品（含库存信息和绑定仓库）

    warehouse_id: 可选，指定仓库ID时返回该仓库的库存数据
    """
    query = session.query(Product)
    query = query.filter(Product.is_archived == False)
    if warehouse_id is not None:
        query = query.filter(Product.default_warehouse_id == warehouse_id)
    if not include_inactive:
        query = query.filter(Product.is_active == True)

    # 如果不包含父级分类，过滤掉父级
    if not include_parents:
        query = query.filter(Product.is_parent == False)

    products = query.all()
    product_order_limits = __import__('product_order_limits').read(session)
    product_units = dict(session.execute(sql_text('SELECT product_id,unit FROM product_units')).all())

    # Read one current snapshot in bounded queries, rather than issuing
    # warehouse/stock/parent SQL again for every catalog row. No cache: edits
    # and live stock are visible on the next request as before.
    warehouse_ids = {p.default_warehouse_id for p in products if p.default_warehouse_id}
    if warehouse_id is not None:
        warehouse_ids.add(warehouse_id)
    warehouses_by_id = {w.id: w for w in session.query(Warehouse).filter(
        Warehouse.id.in_(warehouse_ids)).all()} if warehouse_ids else {}
    product_ids = [p.id for p in products]
    stock_by_key = {(s.warehouse_id, s.product_id): s for s in session.query(WarehouseStock).filter(
        WarehouseStock.product_id.in_(product_ids),
        WarehouseStock.warehouse_id.in_(warehouse_ids)).all()} if product_ids and warehouse_ids else {}
    parent_ids = {p.parent_id for p in products if p.parent_id}
    parents_by_id = {p.id: p for p in session.query(Product).filter(
        Product.id.in_(parent_ids)).all()} if parent_ids else {}

    def _is_archived_catalog_name(name) -> bool:
        return str(name or "").startswith("【历史")

    result = []
    for product in products:
        if _is_archived_catalog_name(product.name):
            continue

        # 获取默认仓库信息
        default_warehouse_name = None
        if product.default_warehouse_id:
            warehouse = warehouses_by_id.get(product.default_warehouse_id)
            if warehouse:
                default_warehouse_name = warehouse.name

        # 获取库存：优先使用 warehouse_id 参数，否则用默认仓库
        stock_quantity = 0
        target_warehouse_id = warehouse_id if warehouse_id is not None else product.default_warehouse_id
        if target_warehouse_id:
            stock = stock_by_key.get((target_warehouse_id, product.id))
            if stock:
                stock_quantity = stock.quantity

        # 获取父级信息
        parent_id = product.parent_id
        parent_name = None
        parent_obj = None
        if parent_id:
            parent_obj = parents_by_id.get(parent_id)
            if parent_obj:
                parent_name = parent_obj.name
                if (not include_inactive and not bool(getattr(parent_obj, "is_active", True))) or bool(parent_obj.is_archived) or not bool(parent_obj.is_parent) or not str(parent_name or '').strip() or _is_archived_catalog_name(parent_name):
                    continue
            else:
                continue
        elif not product.is_parent and not bool(product.is_active):
            continue
        elif not product.is_parent and not parent_id:
            # Orphan flavor/legacy price rows are not a current category.
            continue

        result.append({
            "id": product.id,
            "category_serial": product.category_serial,
            "category_display_id": _display_serial("C", product.category_serial) if product.is_parent else (_display_serial("C", parent_obj.category_serial) if parent_obj else ""),
            "product_serial": product.product_serial,
            "product_display_id": _display_serial("P", product.product_serial) if not product.is_parent else "",
            "name": product.name,
            "cost_price": product.cost_price,
            "reference_price_state": __import__('inventory_reference').price_state(product.cost_price,product.reference_price_state),
            "level1_agent_price": product.level1_agent_price,
            "level2_agent_price": product.level2_agent_price,
            "regular_price": product.regular_price,
            "price": product.regular_price,  # 兼容旧字段，等于regular_price
            "description": product.description,
            "is_active": product.is_active,
            "is_parent": product.is_parent,
            "parent_id": parent_id,
            "parent_name": parent_name,
            "price_offset": __import__('dynamic_prices').offset(product),
            "unit": product_units.get(product.id,product_units.get(product.parent_id,'支')),
            "default_warehouse_id": product.default_warehouse_id,
            "default_warehouse_name": default_warehouse_name,
            "low_stock_threshold": normalize_low_stock_threshold(getattr(product, "low_stock_threshold", None)),
            "max_per_order": product_order_limits.get(product.id),
            "stock": stock_quantity,
            "auto_unlisted": bool(getattr(product, "auto_unlisted", False)),
            "created_at": product.created_at
        })

    return result


@app.get("/api/products/parents")
@_run_blocking_endpoint
def get_parent_categories(
    warehouse_id: Optional[int] = None,
    include_inactive: bool = False,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """获取所有父级商品分类"""
    parents = __import__('catalog_retirement').operating_query(session,Product,'product').filter(
        Product.is_parent == True,
        Product.is_archived == False,
    )
    if not include_inactive:parents=parents.filter(Product.is_active == True)
    if warehouse_id is not None:
        parents = parents.filter(Product.default_warehouse_id == warehouse_id)
    parents = parents.all()

    logical = __import__('logical_categories').groups(session)
    logical_by_parent = {id: group for group in logical for id in group['category_ids']}
    result = []
    for parent in parents:
        # 获取子商品数量
        child_query = __import__('catalog_retirement').operating_query(session,Product,'product').filter(
            Product.parent_id == parent.id,
            Product.is_active == True,
            Product.is_archived == False,
            Product.is_parent == False,
        )
        if warehouse_id is not None:
            child_query = child_query.filter(Product.default_warehouse_id == warehouse_id)
        child_count = child_query.count()

        result.append({
            "id": parent.id,
            "logical_category_id": logical_by_parent.get(parent.id,{'id':parent.id})['id'],
            "logical_category_ids": logical_by_parent.get(parent.id,{'category_ids':[parent.id]})['category_ids'],
            "is_active": bool(parent.is_active),
            "category_serial": parent.category_serial,
            "category_display_id": _display_serial("C", parent.category_serial),
            "name": parent.name,
            "default_warehouse_id": parent.default_warehouse_id,
            "child_count": child_count,
            "cost_price": parent.cost_price,
            "reference_price_state": __import__('inventory_reference').price_state(parent.cost_price,parent.reference_price_state),
            "level1_agent_price": parent.level1_agent_price,
            "level2_agent_price": parent.level2_agent_price,
            "regular_price": parent.regular_price,
            "low_stock_threshold": normalize_low_stock_threshold(getattr(parent, "low_stock_threshold", None)),
            "created_at": parent.created_at
        })

    return result


@app.post("/api/products/migrate_to_default_parent")
@_run_blocking_endpoint
def migrate_products_to_default_parent(session: Session = Depends(get_db_session), _auth: str = Depends(require_admin_or_api_secret)):
    """
    将没有父级的商品迁移到默认父级下
    这个操作只需要执行一次
    """
    # 获取或创建默认父级
    default_parent = session.query(Product).filter(
        Product.name == "默认分类",
        Product.is_parent == True
    ).first()
    if default_parent and default_parent.is_archived:
        raise HTTPException(400,'默认分类已删除，请明确选择有效分类')

    if not default_parent:
        default_parent = Product(
            name="默认分类",
            is_parent=True,
            regular_price=0,
            low_stock_threshold=20,
            category_serial=_next_product_serial(session, True),
        )
        session.add(default_parent)
        session.flush()

    # 找出所有没有父级的商品（不包括父级本身）
    products_without_parent = session.query(Product).filter(
        Product.parent_id.is_(None),
        Product.is_parent == False
    ).all()

    count = 0
    for product in products_without_parent:
        product.parent_id = default_parent.id
        count += 1

    session.commit()

    return {
        "success": True,
        "message": f"已将 {count} 个商品迁移到默认父级分类",
        "default_parent_id": default_parent.id,
        "default_parent_name": default_parent.name,
        "migrated_count": count
    }


@app.post("/api/stock/update")
async def update_stock(
    product_id: int = Form(...),
    quantity: int = Form(...),
    warehouse_id: Optional[int] = Form(None),
    mode: str = Form("set"),
    unit_cost: Optional[float] = Form(None),
    note: Optional[str] = Form(None),
    batch_no: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """更新仓库库存。

    mode:
      - set: 覆盖为 quantity
      - add: 在当前库存上增加 quantity（到货入库，可带本批次成本）
    """
    if not isinstance(note, str):
        note = None
    if not warehouse_id:
        warehouse = session.query(Warehouse).filter(Warehouse.is_active == True).first()
        if not warehouse:
            raise HTTPException(status_code=400, detail="没有可用仓库")
        warehouse_id = warehouse.id

    product = session.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    if product.is_parent:
        raise HTTPException(status_code=400, detail="分类不能直接改库存")
    if product.default_warehouse_id != warehouse_id:
        raise HTTPException(status_code=400, detail="商品只能在绑定仓库维护库存")

    mode = (mode or "set").lower().strip()
    if mode in {"subtract", "remove", "decrease", "out"}:
        amount = int(quantity)
        if amount <= 0:
            raise HTTPException(status_code=400, detail="减少库存数量必须大于0")
        stock = session.query(WarehouseStock).filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id,
        ).first()
        before = int(stock.quantity or 0) if stock else 0
        if amount > before:
            raise HTTPException(status_code=400, detail=f"库存不足，当前 {before}，需要减少 {amount}")
        avg_cost, _cost_amount, _details = db.consume_batches_fifo(session, product_id, warehouse_id, amount)
        if not stock:
            stock = WarehouseStock(warehouse_id=warehouse_id, product_id=product_id, quantity=0)
            session.add(stock)
        stock.quantity = before - amount
        db.add_stock_movement(
            product_id=product_id, warehouse_id=warehouse_id, change_qty=-amount,
            movement_type="adjust", unit_cost=avg_cost, note=note or f"后台减少库存 {amount}",
            session=session,
        )
        db.refresh_product_stock_status(session, product_id)
        session.commit()
        return {
            "success": True, "product_id": product_id, "warehouse_id": warehouse_id,
            "before": before, "after": before - amount, "change": -amount,
            "mode": "subtract",
        }
    if mode == "add":
        result = db.inbound_stock(
            product_id=product_id,
            warehouse_id=warehouse_id,
            quantity=int(quantity),
            unit_cost=unit_cost,
            note=note,
            batch_no=batch_no,
            update_product_cost=True if unit_cost is not None else False,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error") or "入库失败")
        return result

    # Use the atomic delta/FIFO path for absolute quantity changes too. The old
    # direct overwrite path could leave quantity and cost layers inconsistent.
    if mode in {'set','cover','actual'}:
        return await stock_inbound(product_id=product_id,warehouse_id=warehouse_id,
            quantity=int(quantity),mode='actual',unit_cost=unit_cost,note=note,
            batch_no=batch_no,_auth=_auth,session=session)
    raise HTTPException(status_code=400,detail='库存操作类型无效')

@app.post("/api/stock/inbound")
@_run_blocking_endpoint
def stock_inbound(
    product_id: int = Form(...),
    warehouse_id: int = Form(...),
    quantity: int = Form(...),
    mode: str = Form("add"),
    unit_cost: Optional[float] = Form(None),
    note: Optional[str] = Form(None),
    batch_no: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session),
):
    """到货入库：可「增加当前库存」，也可「设为盘点后的实际数量」。

    mode=add（默认，等于改造前的行为）：库存加上 quantity。
    mode=actual/set：库存直接等于 quantity，按差额维护流水；多出来的部分走
    inbound_stock 建批次，少掉的部分按 FIFO 消耗批次，成本账不会算错。
    """
    if not isinstance(note, str):
        note = None
    mode = (mode or "add").strip().lower()
    if mode in {"actual", "set"}:
        target_quantity = int(quantity)
        if target_quantity < 0:
            raise HTTPException(status_code=400, detail="实际数量不能小于0")
        product = session.query(Product).filter(Product.id == product_id).first()
        warehouse = session.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
        if not product or product.is_parent:
            raise HTTPException(status_code=400, detail="商品不存在或分类不能入库")
        if not warehouse:
            raise HTTPException(status_code=400, detail="仓库不存在")
        if product.default_warehouse_id != warehouse_id:
            raise HTTPException(status_code=400, detail="商品只能在绑定仓库入库")

        stock = session.query(WarehouseStock).filter(
            WarehouseStock.product_id == product_id,
            WarehouseStock.warehouse_id == warehouse_id,
        ).first()
        before = int(stock.quantity or 0) if stock else 0
        change = target_quantity - before
        if change > 0:
            result = db.inbound_stock(
                product_id=product_id,
                warehouse_id=warehouse_id,
                quantity=change,
                unit_cost=unit_cost,
                note=note or f"盘点增加至实际数量 {target_quantity}",
                batch_no=batch_no,
                update_product_cost=True if unit_cost is not None else False,
                session=session,
            )
            if not result.get("success"):
                session.rollback()
                raise HTTPException(status_code=400, detail=result.get("error") or "库存校准失败")
        elif change < 0:
            average_cost, _cost_amount, _details = db.consume_batches_fifo(
                session, product_id, warehouse_id, -change
            )
            if not stock:
                stock = WarehouseStock(
                    product_id=product_id,
                    warehouse_id=warehouse_id,
                    quantity=target_quantity,
                )
                session.add(stock)
            else:
                stock.quantity = target_quantity
            db.add_stock_movement(
                product_id=product_id,
                warehouse_id=warehouse_id,
                change_qty=change,
                movement_type="adjust",
                unit_cost=average_cost,
                note=note or f"盘点减少至实际数量 {target_quantity}",
                session=session,
            )
        # 覆盖库存后可售量会变，顺手刷新 TG 停售状态，别让「有货但停售」挂着
        db.refresh_product_stock_status(session, product_id)
        session.commit()
        return {
            "success": True,
            "product_id": product_id,
            "product_name": product.name,
            "warehouse_id": warehouse_id,
            "warehouse_name": warehouse.name,
            "before": before,
            "after": target_quantity,
            "change": change,
            "mode": "actual",
        }
    if mode in {"subtract", "remove", "decrease", "out"}:
        amount = int(quantity)
        if amount <= 0:
            raise HTTPException(status_code=400, detail="减少库存数量必须大于0")
        product = session.query(Product).filter(Product.id == product_id).first()
        warehouse = session.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
        if not product or product.is_parent:
            raise HTTPException(status_code=400, detail="商品不存在或分类不能减少库存")
        if not warehouse or product.default_warehouse_id != warehouse_id:
            raise HTTPException(status_code=400, detail="商品只能在绑定仓库减少库存")
        stock = session.query(WarehouseStock).filter(
            WarehouseStock.product_id == product_id,
            WarehouseStock.warehouse_id == warehouse_id,
        ).first()
        before = int(stock.quantity or 0) if stock else 0
        if amount > before:
            raise HTTPException(status_code=400, detail=f"库存不足，当前 {before}，需要减少 {amount}")
        avg_cost, _cost_amount, _details = db.consume_batches_fifo(session, product_id, warehouse_id, amount)
        stock.quantity = before - amount
        db.add_stock_movement(
            product_id=product_id, warehouse_id=warehouse_id, change_qty=-amount,
            movement_type="adjust", unit_cost=avg_cost, note=note or f"后台减少库存 {amount}",
            session=session,
        )
        db.refresh_product_stock_status(session, product_id)
        session.commit()
        return {
            "success": True, "product_id": product_id, "product_name": product.name,
            "warehouse_id": warehouse_id, "warehouse_name": warehouse.name,
            "before": before, "after": before - amount, "change": -amount, "mode": "subtract",
        }
    if mode != "add":
        raise HTTPException(status_code=400, detail="mode 只支持 add / actual")
    result = db.inbound_stock(
        product_id=product_id,
        warehouse_id=warehouse_id,
        quantity=int(quantity),
        unit_cost=unit_cost,
        note=note,
        batch_no=batch_no,
        update_product_cost=True if unit_cost is not None else False,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "入库失败")
    return result


@app.post("/api/stock/batch_inbound")
@_run_blocking_endpoint
def batch_stock_inbound(
    payload: BatchInboundRequest,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """批量到货入库：先整批校验，再一次性提交。"""
    items = payload.items or []
    if not items:
        raise HTTPException(status_code=400, detail="请提供批量入库数据")

    def resolve_default_parent(warehouse: Warehouse) -> Product:
        default_parent = session.query(Product).filter(
            Product.name == "默认分类",
            Product.is_parent == True,
            Product.default_warehouse_id == warehouse.id,
        ).first()
        if default_parent and default_parent.is_archived:
            raise HTTPException(400,'当前仓库默认分类已删除，请明确选择有效分类')
        if not default_parent:
            default_parent = Product(
                name="默认分类",
                is_parent=True,
                regular_price=0,
                default_warehouse_id=warehouse.id,
                low_stock_threshold=20,
            )
            session.add(default_parent)
            session.flush()
        return default_parent

    results = []
    try:
        for index, item in enumerate(items, start=1):
            line_no = int(item.line_no or index)
            quantity = int(item.quantity or 0)
            if quantity <= 0:
                raise HTTPException(status_code=400, detail=f"第{line_no}行数量必须大于0")

            warehouse = None
            if item.warehouse_id:
                warehouse = session.query(Warehouse).filter(
                    Warehouse.id == int(item.warehouse_id),
                    Warehouse.is_active == True
                ).first()
            elif item.warehouse_name and str(item.warehouse_name).strip():
                warehouse = session.query(Warehouse).filter(
                    Warehouse.name == str(item.warehouse_name).strip(),
                    Warehouse.is_active == True
                ).first()
            if not warehouse:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{line_no}行仓库不存在：{item.warehouse_name or item.warehouse_id}"
                )

            product = None
            if item.product_id:
                product = session.query(Product).filter(
                    Product.id == int(item.product_id),
                    Product.is_parent == False,
                    Product.default_warehouse_id == warehouse.id,
                ).first()
            else:
                product_name = str(item.product_name or "").strip()
                if not product_name:
                    raise HTTPException(status_code=400, detail=f"第{line_no}行口味名称不能为空")
                category_name = str(item.category_name or "").strip()
                if category_name:
                    parent = session.query(Product).filter(
                        Product.name == category_name,
                        Product.is_parent == True,
                        Product.default_warehouse_id == warehouse.id,
                        Product.is_archived == False,
                    ).first()
                    if not parent:
                        raise HTTPException(status_code=400, detail=f"第{line_no}行品类不存在：{category_name}")
                else:
                    parent = resolve_default_parent(warehouse)

                product = session.query(Product).filter(
                    Product.name == product_name,
                    Product.parent_id == parent.id,
                    Product.is_parent == False,
                    Product.default_warehouse_id == warehouse.id,
                ).first()

            if not product:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{line_no}行商品不存在：{item.category_name or ''}/{item.product_name or item.product_id}"
                )

            inbound_cost = item.unit_cost if item.unit_cost is not None else product.cost_price
            result = db.inbound_stock(
                product_id=product.id,
                warehouse_id=warehouse.id,
                quantity=quantity,
                unit_cost=inbound_cost,
                note=item.note,
                update_product_cost=True if item.unit_cost is not None else False,
                session=session,
            )
            if not result.get("success"):
                raise HTTPException(status_code=400, detail=f"第{line_no}行：{result.get('error') or '入库失败'}")

            results.append({
                "line_no": line_no,
                "product_id": product.id,
                "product_name": product.name,
                "category_name": product.parent.name if product.parent else "默认分类",
                "warehouse_id": warehouse.id,
                "warehouse_name": warehouse.name,
                "quantity": quantity,
                "unit_cost": float(result.get("unit_cost") or 0),
                "batch_no": result.get("batch_no"),
            })

        session.commit()
        return {
            "success": True,
            "created_count": len(results),
            "total_quantity": sum(row["quantity"] for row in results),
            "total_amount": round(sum(row["quantity"] * float(row["unit_cost"] or 0) for row in results), 2),
            "items": results,
            "message": f"批量入库成功，共 {len(results)} 条",
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/stock/inbounds")
@_run_blocking_endpoint
def list_stock_inbounds(
    day: Optional[str] = None,
    product_id: Optional[int] = None,
    warehouse_id: Optional[int] = None,
    limit: int = 200,
    _auth: str = Depends(require_admin_or_api_secret),
):
    """查询入库记录：哪天入了什么、多少、什么价格。"""
    return db.list_inbound_records(day=day, product_id=product_id, warehouse_id=warehouse_id, limit=limit)


@app.get("/api/stock/batches")
@_run_blocking_endpoint
def list_stock_batches(
    product_id: Optional[int] = None,
    warehouse_id: Optional[int] = None,
    only_remain: bool = True,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    q = session.query(InventoryBatch)
    if product_id:
        q = q.filter(InventoryBatch.product_id == product_id)
    if warehouse_id:
        q = q.filter(InventoryBatch.warehouse_id == warehouse_id)
    if only_remain:
        q = q.filter(InventoryBatch.remain_qty > 0)
    rows = q.order_by(InventoryBatch.created_at.desc()).limit(300).all()
    return [{
        "id": b.id,
        "batch_no": b.batch_no,
        "product_id": b.product_id,
        "product_name": b.product.name if b.product else None,
        "warehouse_id": b.warehouse_id,
        "warehouse_name": b.warehouse.name if b.warehouse else None,
        "unit_cost": b.unit_cost,
        "cost_source": b.cost_source,
        "cost_verified": b.cost_source in ('VERIFIED_REAL_COST', 'RESTORED_VERIFIED_FIFO'),
        "quantity": b.quantity,
        "remain_qty": b.remain_qty,
        "amount": float(b.remain_qty or 0) * float(b.unit_cost or 0),
        "note": b.note,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    } for b in rows]


@app.post("/api/stock/delete")
@_run_blocking_endpoint
def delete_stock_row(
    warehouse_id: int = Form(...),
    product_id: int = Form(...),
    _auth: str = Depends(require_admin_or_api_secret),
):
    result = db.delete_stock_row(warehouse_id, product_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "删除失败")
    return result


@app.post("/api/products/batch_clear_stock")
@_run_blocking_endpoint
def batch_clear_product_stock(
    product_ids: str = Form(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """批量清空所选口味库存，保留商品和库存流水。"""
    try:
        ids = sorted({int(value) for value in str(product_ids).split(',') if value.strip()})
    except ValueError:
        raise HTTPException(status_code=400, detail="商品ID格式不正确")
    if not ids:
        raise HTTPException(status_code=400, detail="请先勾选商品口味")
    products = session.query(Product).filter(
        Product.id.in_(ids),
        Product.is_parent == False,
        Product.is_archived == False,
    ).all()
    found_ids = {int(product.id) for product in products}
    missing = [product_id for product_id in ids if product_id not in found_ids]
    if missing:
        raise HTTPException(status_code=400, detail=f"存在无效商品ID：{','.join(map(str, missing))}")
    cleared_qty = 0
    cleared_rows = 0
    try:
        for product in products:
            stocks = session.query(WarehouseStock).filter(WarehouseStock.product_id == product.id).all()
            for stock in stocks:
                quantity = int(stock.quantity or 0)
                if quantity:
                    cleared_qty += quantity
                    session.add(StockMovement(
                        product_id=product.id,
                        warehouse_id=stock.warehouse_id,
                        change_qty=-quantity,
                        movement_type="delete",
                        note="后台批量删除库存清零",
                    ))
                session.delete(stock)
                cleared_rows += 1
            session.query(InventoryBatch).filter(
                InventoryBatch.product_id == product.id,
                InventoryBatch.remain_qty > 0,
            ).update({InventoryBatch.remain_qty: 0}, synchronize_session=False)
            db.refresh_product_stock_status(session, product.id)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": True,
        "product_count": len(products),
        "cleared_stock_rows": cleared_rows,
        "cleared_qty": cleared_qty,
        "message": f"已清空 {len(products)} 个口味的库存，共 {cleared_qty} 件",
    }


@app.post("/api/products/batch_archive")
@_run_blocking_endpoint
def batch_archive_products(
    product_ids: str = Form(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """批量软删除口味；历史订单仍保留原商品名称和关联。"""
    try:
        ids = sorted({int(value) for value in str(product_ids).split(',') if value.strip()})
    except ValueError:
        raise HTTPException(status_code=400, detail="商品ID格式不正确")
    if not ids:
        raise HTTPException(status_code=400, detail="请先勾选商品口味")
    products = session.query(Product).filter(
        Product.id.in_(ids),
        Product.is_parent == False,
        Product.is_archived == False,
    ).all()
    found_ids = {int(product.id) for product in products}
    missing = [product_id for product_id in ids if product_id not in found_ids]
    if missing:
        raise HTTPException(status_code=400, detail=f"存在无效商品ID：{','.join(map(str, missing))}")
    stock_rows = session.query(WarehouseStock.product_id, WarehouseStock.quantity).filter(
        WarehouseStock.product_id.in_(ids),
        WarehouseStock.quantity > 0,
    ).all()
    if stock_rows:
        blocked = sorted({int(row[0]) for row in stock_rows})
        raise HTTPException(
            status_code=400,
            detail=f"以下商品还有库存，请先批量删除库存：{','.join(map(str, blocked))}",
        )
    try:
        for product in products:
            product.is_active = False
            product.auto_unlisted = False
            product.is_archived = True
        session.query(WarehouseStock).filter(
            WarehouseStock.product_id.in_(ids),
        ).delete(synchronize_session=False)
        session.query(InventoryBatch).filter(
            InventoryBatch.product_id.in_(ids),
        ).update({InventoryBatch.remain_qty: 0}, synchronize_session=False)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": True,
        "archived_count": len(products),
        "message": f"已删除 {len(products)} 个口味，历史订单不受影响",
    }


@app.post("/api/warehouses/{warehouse_id}/delete")
@_run_blocking_endpoint
def delete_warehouse(
    request: Request,
    warehouse_id: int,
    _auth: str = Depends(require_admin_or_api_secret),
):
    _warehouse_settings_guard(request,_auth)
    result = db.delete_warehouse(warehouse_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "删除失败")
    return result


def _warehouse_settings_guard(request, auth):
    if auth!='session' or request.session.get('admin_username')!=ADMIN_USERNAME:
        raise HTTPException(403,'仅管理员可修改仓库配置')
    expected=request.session.get('category_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Category-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面')


@app.get('/api/warehouse-routing-policy')
@_run_blocking_endpoint
def warehouse_routing_get(session:Session=Depends(get_db_session),_auth:str=Depends(require_admin_or_api_secret)):
    from warehouse_policy import load
    return load(session)


@app.post('/api/warehouse-routing-policy')
@_run_blocking_endpoint
def warehouse_routing_save(request:Request,payload:dict,session:Session=Depends(get_db_session),
                           _auth:str=Depends(require_admin_or_api_secret)):
    _warehouse_settings_guard(request,_auth)
    from warehouse_policy import save
    try:return save(session,payload)
    except ValueError as error:raise HTTPException(409,str(error)) from None


@app.get("/api/stats/shipments")
@_run_blocking_endpoint
def stats_shipments(start: str, end: str, warehouse_id: Optional[int] = None,
                          _auth: str = Depends(require_admin_or_api_secret)):
    from shipment_statistics import calculate
    try:
        return calculate(db, start, end, warehouse_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/stats/shipments/export")
@_run_blocking_endpoint
def export_shipment_cohort(start: str, end: str, warehouse_id: Optional[int] = None,
                                session: Session = Depends(get_db_session),
                                _auth: str = Depends(require_admin_or_api_secret)):
    from shipment_statistics import bounds
    try:
        first, last = bounds(start, end)
        if warehouse_id is not None and not session.get(Warehouse, warehouse_id):
            raise ValueError('仓库不存在')
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    query=session.query(Order).options(selectinload(Order.user),
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent),
        selectinload(Order.items).selectinload(OrderItem.warehouse))
    query=query.filter(Order.created_at >= first, Order.created_at < last, Order.status != 'cancelled')
    if warehouse_id is not None:query=query.filter(Order.items.any(OrderItem.warehouse_id == warehouse_id))
    orders=query.order_by(Order.created_at,Order.id).all()
    if warehouse_id is not None and any(any(i.warehouse_id!=warehouse_id for i in o.items) for o in orders):
        raise HTTPException(status_code=400,detail='历史订单包含多仓明细，请使用全部仓库查询')
    from business_time_range import OFFSET
    from datetime import timedelta
    filename='shipping_'+re.sub(r'[^0-9A-Za-z_-]','-',start+'_'+end)+'.csv'
    return _build_shipping_orders_csv_response(orders,first+OFFSET,last+OFFSET-timedelta(microseconds=1),filename,session=session)


@app.get("/api/stats/period")
@_run_blocking_endpoint
def stats_period(
    period: str = "day",
    ref: Optional[str] = None,
    _auth: str = Depends(require_admin_or_api_secret),
):
    """日/月/季度统计：盈利、入库成本、仓库剩余货值。"""
    return db.period_stats(period=period, ref=ref)


@app.get("/api/stats/sales_breakdown")
@_run_blocking_endpoint
def stats_sales_breakdown(
    period: str = "day",
    ref: Optional[str] = None,
    shipped_only: bool = False,
    _auth: str = Depends(require_admin_or_api_secret),
):
    """按日/月/季度返回品类、口味销售数量明细。"""
    period = (period or "day").lower()
    if period not in {"day", "month", "quarter"}:
        raise HTTPException(status_code=400, detail="统计周期必须是 day、month 或 quarter")
    if shipped_only:
        return db.sales_breakdown(period=period, ref=ref, shipped_only=True)
    return db.sales_breakdown(period=period, ref=ref)


@app.get("/api/stats/inventory_value")
@_run_blocking_endpoint
def stats_inventory_value(_auth: str = Depends(require_admin_or_api_secret)):
    return db.inventory_value_summary()


@app.get("/api/finance/overdue")
@_run_blocking_endpoint
def finance_overdue(
    days: int = 2,
    _auth: str = Depends(require_admin_or_api_secret),
):
    """超过 N 天未付清预警，默认 2 天。"""
    return db.overdue_unpaid_orders(days=days)


async def _product_inline_value(request: Request, value: str = Form("")):
    # An explicitly empty value clears optional settings. A missing field
    # must not silently reset prices or limits.
    if 'value' not in await request.form():
        raise HTTPException(status_code=422, detail="缺少value字段")
    return value


@app.post("/api/products/{product_id}/inline_update")
@_run_blocking_endpoint
def inline_update_product(
    product_id: int,
    field: str = Form(...),
    value: str = Depends(_product_inline_value),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
    price_review_token: str = Form(''),
):
    """商品表格点击即改：name/cost_price/level1/level2/regular/parent_id/warehouse_id/is_active/low_stock_threshold"""
    if field in ('default_warehouse_id','max_per_order') and not session.in_transaction():
        session.execute(sql_text('BEGIN IMMEDIATE'))
    product = session.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    field = (field or "").strip()
    allowed = {
        "name", "cost_price", "level1_agent_price", "level2_agent_price",
        "regular_price", "parent_id", "default_warehouse_id", "is_active", "description", "low_stock_threshold", "max_per_order"
    }
    if field not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持字段: {field}")
    try:
        if field=='max_per_order':
            try:
                maximum=__import__('product_order_limits').set_limit(session,product,value,_auth)
            except ValueError as exc:raise HTTPException(400,str(exc)) from None
            session.commit()
            return {'success':True,'product_id':product_id,'field':field,'value':maximum}
        if field == "name":
            name = value.strip()
            if not name:
                raise HTTPException(status_code=400, detail="名称不能为空")
            exists = session.query(Product).filter(
                Product.name == name,
                Product.parent_id == product.parent_id,
                Product.is_parent == bool(product.is_parent),
                Product.id != product_id,
            ).first()
            if exists:
                raise HTTPException(status_code=400, detail="该型号下已存在同名口味")
            try:__import__('catalog_rename').record_rename(session,product,name,_auth)
            except ValueError as error:raise HTTPException(400,str(error)) from None
            product.name = name
        elif field == 'cost_price':
            raw=value.strip()
            price=float(raw) if raw else 0.0
            if not __import__('math').isfinite(price) or price<0:
                raise HTTPException(400,'参考成本必须为有限非负数')
            product.cost_price=price
            product.reference_price_state='SET' if raw else 'UNSET'
        elif field in ("level1_agent_price", "level2_agent_price", "regular_price"):
            __import__('sale_price_review').catalog_confirmation(session,[product],{field:value},price_review_token,
                dict(route='catalog_inline',product_id=product_id))
            setattr(product, field, __import__('sale_price_review').amount(value))
        elif field == "parent_id":
            new_parent_id = int(value) if str(value).strip() not in ("", "null", "None") else None
            if new_parent_id:
                parent = session.query(Product).filter(Product.id == new_parent_id, Product.is_parent == True).first()
                if not parent:
                    raise HTTPException(status_code=400, detail="指定的父级分类不存在")
            exists = session.query(Product).filter(
                Product.name == product.name,
                Product.parent_id == new_parent_id,
                Product.is_parent == False,
                Product.id != product_id,
            ).first()
            if exists:
                raise HTTPException(status_code=400, detail="目标型号下已存在同名口味")
            product.parent_id = new_parent_id
        elif field == "default_warehouse_id":
            target_warehouse = int(value) if str(value).strip() not in ("", "null", "None") else None
            if product.is_parent:
                from category_warehouse import apply_move
                apply_move(session,product.id,target_warehouse,product.name)
            elif product.default_warehouse_id!=target_warehouse:
                if product.parent and product.parent.default_warehouse_id!=target_warehouse:
                    raise HTTPException(400,'口味所属仓库必须与分类一致，请调整分类仓库')
                if session.query(WarehouseStock).filter(WarehouseStock.product_id==product.id,WarehouseStock.quantity!=0).first() or session.query(InventoryBatch).filter(InventoryBatch.product_id==product.id,InventoryBatch.remain_qty>0).first():
                    raise HTTPException(400,'商品存在库存或未消耗成本批次，禁止直接换仓')
                warehouse=session.get(Warehouse,target_warehouse)
                if not warehouse or not warehouse.is_active:raise HTTPException(400,'目标仓库不可用')
                product.default_warehouse_id=target_warehouse
        elif field == "low_stock_threshold":
            product.low_stock_threshold = normalize_low_stock_threshold(value, product.low_stock_threshold)
        elif field == "is_active":
            product.is_active = str(value).lower() in ("1", "true", "yes", "on")
            product.auto_unlisted = False
        elif field == "description":
            product.description = value
        if field in ("low_stock_threshold", "is_active"):
            db.refresh_product_stock_status(session, product.id)
        session.commit()
        return {"success": True, "product_id": product_id, "field": field, "value": getattr(product, field)}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))



@app.get("/api/stock")
@_run_blocking_endpoint
def get_stock(
    warehouse_id: Optional[int] = None,
    product_id: Optional[int] = None,
    include_inactive: bool = False,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """获取库存信息"""
    if warehouse_id and product_id:
        stock = session.query(WarehouseStock).filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id
        ).first()
        if not stock or stock.product.default_warehouse_id != warehouse_id:
            raise HTTPException(status_code=404, detail="库存记录不存在")
        if not include_inactive and (not stock.product.is_active or stock.product.is_archived or
                not stock.product.parent or not stock.product.parent.is_active or stock.product.parent.is_archived):
            raise HTTPException(status_code=404, detail="商品已下架")
        return {
            "warehouse_id": stock.warehouse_id,
            "warehouse_name": stock.warehouse.name,
            "product_id": stock.product_id,
            "product_display_id": _display_serial("P", stock.product.product_serial),
            "product_name": stock.product.name,
            "parent_id": stock.product.parent_id,
            "category_display_id": _display_serial("C", stock.product.parent.category_serial) if stock.product.parent else "",
            "parent_name": stock.product.parent.name if stock.product.parent else None,
            "low_stock_threshold": normalize_low_stock_threshold(getattr(stock.product, "low_stock_threshold", None)),
            "is_active": bool(stock.product.is_active),
            "auto_unlisted": bool(getattr(stock.product, "auto_unlisted", False)),
            "quantity": stock.quantity,
            "unit": __import__('product_units').effective(session,stock.product),
            **_stock_view(stock.quantity, getattr(stock.product, "low_stock_threshold", None)),
            "last_updated": stock.last_updated
        }
    else:
        stocks = session.query(WarehouseStock, Warehouse, Product).join(
            Warehouse, WarehouseStock.warehouse_id == Warehouse.id
        ).join(
            Product, WarehouseStock.product_id == Product.id
        ).filter(
            Warehouse.is_active == True,
            Product.default_warehouse_id == WarehouseStock.warehouse_id,
            Product.is_archived == False,
        )

        if not include_inactive:
            stocks = stocks.filter(Product.is_active == True)

        if warehouse_id:
            stocks = stocks.filter(WarehouseStock.warehouse_id == warehouse_id)
        if product_id:
            stocks = stocks.filter(WarehouseStock.product_id == product_id)

        stocks = stocks.all()

        result = []
        for stock in stocks:
            prod = stock.Product
            if not include_inactive and (not prod.parent or not prod.parent.is_active or prod.parent.is_archived):
                continue
            parent_name = ''
            try:
                if prod.parent_id:
                    parent = session.query(Product).filter(Product.id == prod.parent_id).first()
                    parent_name = parent.name if parent else ''
            except Exception:
                parent_name = ''
            qty = stock.WarehouseStock.quantity or 0
            cost = float(getattr(prod, 'cost_price', 0) or 0)
            result.append({
                "warehouse_id": stock.WarehouseStock.warehouse_id,
                "warehouse_name": stock.Warehouse.name,
                "product_id": stock.WarehouseStock.product_id,
                "product_display_id": _display_serial("P", prod.product_serial),
                "product_name": prod.name,
                "category_display_id": _display_serial("C", prod.parent.category_serial) if prod.parent else "",
                "parent_name": parent_name,
                "parent_id": prod.parent_id,
                "low_stock_threshold": normalize_low_stock_threshold(getattr(prod, "low_stock_threshold", None)),
                "is_active": bool(prod.is_active),
                "auto_unlisted": bool(getattr(prod, "auto_unlisted", False)),
                "cost_price": cost,
                "regular_price": float(getattr(prod, 'regular_price', 0) or 0),
                "price": float(getattr(prod, 'regular_price', 0) or getattr(prod, 'price', 0) or 0),
                "quantity": qty,
                "unit": __import__('product_units').effective(session,prod),
                **_stock_view(qty, getattr(prod, "low_stock_threshold", None)),
                "value": round(qty * cost, 2),
                "last_updated": stock.WarehouseStock.last_updated.isoformat() if getattr(stock.WarehouseStock, 'last_updated', None) else None,
            })
        return result


def _stock_view(quantity, threshold) -> dict:
    """给库存响应补上「可售 / 预留」两个数，实际库存仍在 quantity 字段。

    后台需要同时看到三个数才能判断补货：实际 1000 / 可售 800 / 预留 200。
    机器人和客户只看 sellable_quantity。
    """
    import config as _cfg
    from stock_rules import stock_view as _sv
    view = _sv(quantity, threshold,
               getattr(_cfg, 'STOCK_AUTO_UNLIST_MODE', 'threshold'))
    return {
        'sellable_quantity': view['sellable_quantity'],
        'reserved_quantity': view['reserved_quantity'],
    }


def _parse_product_reference(text_value: str):
    """解析 品类(鸭子)口味(苹果) / 型号(鸭子)口味(苹果) / 鸭子 苹果。"""
    import re as _re
    text_value = (text_value or "").strip()
    if not text_value:
        return None, None
    patterns = [
        r"(?:品类|型号)\(([^)]+)\)\s*口味\(([^)]+)\)",
        r"(?:品类|型号)[:：]?\s*([^\s,，]+)\s+口味[:：]?\s*([^\s,，]+)",
    ]
    for pattern in patterns:
        match = _re.search(pattern, text_value)
        if match:
            return match.group(1).strip(), match.group(2).strip()
    parts = text_value.split()
    if len(parts) >= 2:
        return parts[0].strip(), parts[-1].strip()
    return None, text_value


def _resolve_order_product(session: Session, item: dict, user=None):
    product_id = item.get("product_id")
    if product_id:
        product = session.query(Product).filter(
            Product.id == int(product_id),
            Product.is_parent == False,
        ).first()
        if product:
            return product
        raise HTTPException(status_code=400, detail=f"商品不存在: ID {product_id}")

    raw_name = (item.get("product_name") or item.get("name") or "").strip()
    parent_name = (item.get("parent_name") or item.get("category") or item.get("model") or "").strip()
    parsed_parent, parsed_flavor = _parse_product_reference(raw_name)
    if parsed_parent:
        parent_name = parsed_parent
    product_name = (item.get("flavor") or parsed_flavor or raw_name).strip()
    if not product_name:
        raise HTTPException(status_code=400, detail="商品名称不能为空")

    query = session.query(Product).filter(
        Product.name == product_name,
        Product.is_parent == False,
    )
    if parent_name:
        parents = session.query(Product).filter(
            Product.name == parent_name,
            Product.is_parent == True,
        ).all()
        if not parents:
            raise HTTPException(status_code=400, detail=f"品类不存在: {parent_name}")
        query = query.filter(Product.parent_id.in_([p.id for p in parents]))
    rows = query.all()
    if user is not None:
        rows=[p for p in rows if not _is_private_warehouse(p.default_warehouse)
              or _username_token(user.username)==WHOLESALE_PRIVATE_USERNAME]
    if not rows:
        if parent_name:
            raise HTTPException(status_code=400, detail=f"商品不存在: 品类({parent_name})口味({product_name})")
        raise HTTPException(status_code=400, detail=f"商品不存在: {product_name}")
    if len(rows) > 1:
        identities={(p.parent.name if p.parent else None,p.name) for p in rows}
        warehouses={p.default_warehouse_id for p in rows}
        if len(identities)==1 and None not in warehouses and len(warehouses)==len(rows):
            active=[p for p in rows if p.is_active and p.parent and p.parent.is_active]
            return min(active or rows,key=lambda p:p.id)
        raise HTTPException(status_code=400, detail=f"口味 '{product_name}' 存在多个型号，请按 品类(型号)口味({product_name})x数量 填写")
    return rows[0]


@app.post("/api/orders/create")
@__import__('order_trace').api_traced
async def create_order_api(
    telegram_id: str = Form(...),
    items: str = Form(...),  # JSON字符串: [{"product_name": "苹果", "quantity": 2, "warehouse_name": "北京仓库"}, ...]
    recipient_name: str = Form(...),
    recipient_phone: str = Form(...),
    recipient_address: str = Form(...),
    notes: Optional[str] = Form(None),
    source_chat_id: Optional[str] = Form(None),
    source_message_id: Optional[int] = Form(None),
    source_chat_type: Optional[str] = Form(None),
    source_chat_title: Optional[str] = Form(None),
    expected_quote: Optional[str] = Form(None),
    automatic_order: bool = Form(False),
    cross_warehouse_confirmed: bool = Form(False),
    explicit_warehouse_id: Optional[int] = Form(None),
    diagnostic_trace_id: Optional[str] = Form(None),
    bundle_requests: Optional[str] = Form(None),
    execution_credentials: Optional[str] = Form(None),
    proxy_confirmation: Optional[str] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """创建订单（供Telegram Bot调用）"""
    import json
    proxy_info=None
    if isinstance(proxy_confirmation,str):
        try:proxy_info=json.loads(proxy_confirmation)
        except (ValueError,TypeError):raise HTTPException(400,'代确认凭据无效') from None
        if not isinstance(proxy_info,dict) or set(proxy_info)!={'actor_id','draft_token'}:raise HTTPException(400,'代确认凭据无效')
    execution_info=None
    if isinstance(execution_credentials,str):
        try:execution_info=__import__('json').loads(execution_credentials)
        except (ValueError,TypeError):raise HTTPException(400,'INVALID_EXECUTION_CREDENTIALS') from None
    # 获取或创建用户
    user = session.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 群自动录单是代发能力，服务端必须再次校验最新授权状态，不能只依赖 Bot
    # 侧的预检查，否则任何持有 API 密钥的调用方都能绕过群订单门禁。
    source_chat_type = str(source_chat_type or '').strip().lower() or None
    is_group_order = source_chat_id is not None and source_chat_type in {'group', 'supergroup'}
    if source_chat_id is not None:
        try:
            negative_chat = int(source_chat_id) < 0
        except (ValueError, TypeError):
            raise HTTPException(403, '接单群来源无效') from None
        if negative_chat and not is_group_order:
            raise HTTPException(403, '接单群必须提供完整消息来源')
    if is_group_order and (source_message_id is None or source_message_id <= 0):
        raise HTTPException(403, '接单群必须提供原始消息编号')
    if automatic_order is True:
        if not source_chat_id or not source_message_id or source_message_id <= 0:
            raise HTTPException(400, '自动录单必须提供原始消息来源')
        if source_chat_type == 'private' and str(source_chat_id) != str(telegram_id):
            raise HTTPException(403, '私聊订单消息发送人与账户不一致')
        if source_chat_type not in {'private','group','supergroup'}:
            raise HTTPException(400, '自动录单消息类型无效')
    import telegram_accounts
    if not telegram_accounts.eligible(session,user):
        raise HTTPException(403,'请先通过代发申请审核后下单；历史订单和账务仍可查看')
    if is_group_order:
        auth_request = (
            session.query(DropshipAuthRequest)
            .filter(DropshipAuthRequest.user_id == user.id)
            .order_by(DropshipAuthRequest.created_at.desc(), DropshipAuthRequest.id.desc())
            .first()
        )
        if not auth_request or auth_request.status != 'approved':
            status = auth_request.status if auth_request else 'none'
            detail = {
                'none': '请先申请代发下单权限',
                'pending': '代发授权申请正在等待后台审核',
                'rejected': '代发授权未通过，群订单不会被识别',
            }.get(status, '代发下单权限未通过，群订单不会被识别')
            raise HTTPException(status_code=403, detail=detail)

    # 异常地址黑名单拦截
    blocked, hit = db.is_address_blocked(recipient_address)
    if blocked:
        reason = hit.reason if hit and hit.reason else "地址异常"
        from blocked_regions import rejection
        raise HTTPException(status_code=403, detail=rejection(hit))

    # Keep the canonical address in the order record.  This is also applied to
    # direct API callers that bypass the Telegram preview/DeepSeek path.
    try:
        _address_meta = normalize_cn_address(recipient_address)
        _canonical_address = str(
            _address_meta.get('normalized_address') or recipient_address
        ).strip()
        if _canonical_address:
            recipient_address = _canonical_address
    except Exception:
        pass

    # 解析商品列表
    import json
    try:
        items_list = json.loads(items)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="商品数据格式错误")

    # 转换商品名称为ID
    bundle_list=[]
    if isinstance(bundle_requests,str):
        try:
            bundle_list=json.loads(bundle_requests)
            if not isinstance(bundle_list,list) or any(not isinstance(row,dict) for row in bundle_list):
                raise ValueError()
        except (ValueError,TypeError):raise HTTPException(400,'福袋数据格式无效') from None
        if bundle_list and not inspect_database(db.engine).has_table('bundle_definitions'):
            raise HTTPException(503,'福袋数据库升级尚未完成')
    if not isinstance(items_list,list) or (not items_list and not bundle_list) or any(not isinstance(row,dict) for row in items_list):
        raise HTTPException(status_code=400, detail='商品数据必须为非空列表')
    accepted_quote = None
    if isinstance(expected_quote, str):
        try:
            accepted_quote = json.loads(expected_quote)
            from order_result import monetary_terms
            monetary_terms(accepted_quote)
        except (ValueError, TypeError, KeyError):
            raise HTTPException(status_code=400, detail='用户确认金额无效，请重新发送订单') from None
    if not inspect_database(db.engine).has_table('order_allocations'):
        raise HTTPException(status_code=503, detail='自动分仓Migration尚未完成，暂不接受新订单')
    order_items = []
    for item in items_list:
        product = _resolve_order_product(session, item, user=user)
        display_name = product.get_full_name() if hasattr(product, "get_full_name") else product.name
        if not product.is_active:
            raise HTTPException(status_code=400, detail=f"商品已下架，不能下单: {display_name}")
        if product.parent and not product.parent.is_active:
            raise HTTPException(status_code=400, detail=f"商品分类已下架，不能下单: {product.parent.name}")
        warehouse=session.get(Warehouse,product.default_warehouse_id)
        if not warehouse:
            raise HTTPException(status_code=400,detail='商品没有真实绑定仓库')
        _ensure_order_warehouse_access(user, warehouse, product)
        try:
            quantity=int(item.get('quantity',0))
        except (ValueError,TypeError):
            raise HTTPException(status_code=400,detail='商品数量无效') from None
        if quantity<=0:
            raise HTTPException(status_code=400,detail='商品数量必须大于0')
        # Final sellable-stock check and daily balancing happen under one write lock.
        order_items.append({
            "product_id": product.id,
            "quantity": quantity,
            "warehouse_id": warehouse.id,
            "category_id": product.parent_id,
        })

    # 同地址不限制总支数；每 N 支在数据库中生成一笔正式子订单。
    _limits = _packaging_limits()
    _units = package_rules.total_units(order_items)
    if _units <= 0 and not bundle_list:
        raise HTTPException(status_code=400, detail='订单商品数量必须大于0')
    _split_units = int(_limits.get('max_units_per_order') or _limits.get('units_per_package') or 6)

    # 创建订单：SQLite 偶发写锁时短暂重试，避免直接暴露 500
    order = None
    for attempt in range(3):
        try:
            # create_order owns its own session and transaction. A SQLite
            # writer wait must not block the API event loop or unrelated reads.
            order = await asyncio.to_thread(db.create_order,
                user_id=user.id,
                items=order_items,
                recipient_name=recipient_name,
                recipient_phone=recipient_phone,
                recipient_address=recipient_address,
                notes=notes,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                source_chat_type=source_chat_type,
                source_chat_title=source_chat_title,
                split_units=_split_units,
                auto_allocate=True,
                expected_quote=accepted_quote,
                qualification_required=True,
                automatic_order=automatic_order is True,
                cross_warehouse_confirmed=cross_warehouse_confirmed is True,
                explicit_warehouse_id=explicit_warehouse_id if isinstance(explicit_warehouse_id,int) else None,
                bundle_requests=bundle_list or None,
                execution_info=execution_info,
                proxy_info=proxy_info,
            )
            break
        except ValueError as e:
            from warehouse_allocation import CrossWarehouseRequired
            if isinstance(e, CrossWarehouseRequired):
                raise HTTPException(409, detail={'code':'CROSS_WAREHOUSE_REQUIRED',
                    'message':str(e),'order_created':False,'stock_changed':False}) from None
            from order_result import ReconfirmationRequired
            if isinstance(e, ReconfirmationRequired):
                raise HTTPException(status_code=409, detail={
                    'code':'RECONFIRM_REQUIRED', 'message':str(e), 'quote':e.quote,
                    'order_created':False, 'stock_changed':False,
                }) from None
            msg = str(e)
            if msg.startswith('QUALIFICATION_REQUIRED:'):
                raise HTTPException(403, msg.split(':', 1)[1]) from None
            if msg.startswith('MESSAGE_ALREADY_PROCESSED:'):
                raise HTTPException(409,'该群订单已处理，请查看下单记录') from None
            if msg.startswith("BLOCKED_ADDRESS:"):
                raise HTTPException(status_code=403, detail=msg.replace("BLOCKED_ADDRESS:", "", 1))
            raise HTTPException(status_code=400, detail=msg)
        except OperationalError as e:
            if "database is locked" not in str(e).lower():
                raise
            if attempt >= 2:
                raise HTTPException(status_code=503, detail="系统繁忙，请稍后重试")
            await asyncio.sleep(0.25 * (attempt + 1))

    if not order:
        raise HTTPException(status_code=400, detail="创建订单失败，可能是库存不足、余额不足或地址异常")

    created_orders = list(getattr(order, 'created_orders', None) or [order])
    created_order_ids = [created.id for created in created_orders]

    # create_order 在另一会话提交，这里需要刷新，避免读到旧库存/旧余额
    session.expire_all()

    # Snapshot taken by the committing transaction, never a preview warehouse
    # or a later stock read that could include another concurrent order.
    remaining_stock = order.final_remaining_stock

    # 重新读取余额
    user = session.query(User).filter(User.id == user.id).first()

    persisted_orders = session.query(Order).options(
        selectinload(Order.user),
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent),
        selectinload(Order.items).selectinload(OrderItem.warehouse),
    ).filter(Order.id.in_(created_order_ids)).order_by(Order.id.asc()).all()

    # 订单已落库后通知客服。通知失败只记录日志，绝不回滚订单。
    try:
        # Order success has one Bot receipt. Entering pending is not a
        # payment event and must not emit a second customer bill notice.
        notification_targets = resolve_order_notification_targets(session)
        if notification_targets:
            for notify_order in persisted_orders:
                await asyncio.to_thread(
                    send_new_order_notifications,
                    build_new_order_notify_text(notify_order),
                    notification_targets,
                )
    except Exception as exc:
        logger.exception('新订单客服通知失败 %s: %s', order.order_number, exc)

    split_notice = {'attempted': False, 'sent': False, 'sent_count': 0, 'failed_count': 0}
    # Durable per-waiting-child reminders replace the old whole-group alert.
    # The periodic worker owns recovery; slow Telegram never holds the receipt.
    asyncio.create_task(_sync_split_notices())

    with __import__('order_trace').stage('RECEIPT_DEBT_READ'):
        debt = db.calc_user_debt(user.id, session=session)
    full_total_amount = sum(float(item.total_amount or 0) for item in persisted_orders)
    full_shipping_fee = sum(float(item.total_shipping_fee or 0) for item in persisted_orders)
    full_grand_total = sum(float(item.grand_total or 0) for item in persisted_orders)
    from order_receipt import committed_daily_summary
    with __import__('order_trace').stage('RECEIPT_DAILY_READ'):
        daily_summary = committed_daily_summary(session, user.id, receipt_only=automatic_order)
    order_summaries = [{
        'order_id': item.id,
        'order_number': item.order_number,
        'status': item.status,
        'split_index': item.split_index,
        'split_count': item.split_count,
        'total_amount': item.total_amount,
        'total_shipping_fee': item.total_shipping_fee,
        'grand_total': item.grand_total,
    } for item in persisted_orders]
    return {
        "success": True,
        "order_id": order.id,
        "order_number": order.order_number,
        "order_ids": created_order_ids,
        "order_numbers": [item.order_number for item in persisted_orders],
        "display_order_number": _public_order_number(order),
        "display_order_numbers": [_public_order_number(item) for item in persisted_orders],
        "orders": order_summaries,
        "is_split": len(persisted_orders) > 1,
        "split_group_id": getattr(order, 'split_group_id', None),
        "split_count": len(persisted_orders),
        "package_count": package_rules.final_package_count(
            [item for saved in persisted_orders for item in saved.items]),
        "split_notice": split_notice,
        "total_amount": full_total_amount,
        "total_shipping_fee": full_shipping_fee,
        "grand_total": full_grand_total,
        "paid_amount": getattr(order, 'paid_amount', 0) or 0,
        "payment_status": getattr(order, 'payment_status', 'unpaid'),
        "user_balance": user.balance,
        "user_debt": debt,
        "daily_summary": daily_summary,
        "bundle_sales": __import__('bundles').receipt_sales(session,created_order_ids) if bundle_list else [],
        "remaining_stock": remaining_stock,
        "items": [__import__('order_result').persisted_item(session,item,
            next((r['remaining_quantity'] for r in remaining_stock
                  if r['product_id']==item.product_id and r['warehouse_id']==item.warehouse_id),None))
            for saved in persisted_orders for item in saved.items],
        "recipient_name": order.recipient_name,
        "recipient_phone": order.recipient_phone,
        "recipient_address": order.recipient_address,
        "source_chat_id": order.source_chat_id,
        "source_message_id": order.source_message_id,
        "source_chat_type": order.source_chat_type,
        "source_chat_title": order.source_chat_title,
        "created_at": order.created_at,
        "message": (
            f"下单成功，已自动拆为 {len(persisted_orders)} 笔并等待人工确认"
            if len(persisted_orders) > 1
            else "下单成功（未强制扣款，已记入欠款，等待人工收账）"
        ),
    }


def _parse_order_ids(order_ids: str) -> list:
    ids = []
    for part in (order_ids or "").replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            if int(part)>0 and int(part) not in ids:
                ids.append(int(part))
    return ids


@app.post("/api/orders/batch_update_status")
async def batch_update_order_status(
    order_ids: str = Form(...),
    status: str = Form(...),
    reason: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session),
    selection_token: Optional[str] = Form(None),
):
    """Batch confirm/ship/cancel selected orders."""
    if status not in ("confirmed", "shipped", "cancelled"):
        raise HTTPException(status_code=400, detail="批量操作仅支持确认、发货或取消")
    if status == "cancelled":
        reject_api_key_direct_cancel(_auth)
        reason = require_cancellation_reason(reason)
    ids = _parse_order_ids(order_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="请选择订单")
    if len(ids)>200:
        raise HTTPException(400,'单次最多200笔，请由后台逐批处理明确选择的订单')

    snapshot = None
    if isinstance(selection_token, str) and selection_token:
        import order_selection
        try: snapshot = order_selection.decode(selection_token, SESSION_SECRET_KEY)
        except ValueError as exc: raise HTTPException(400, str(exc))
        versions = dict(snapshot['orders'])
        if not set(ids).issubset(versions):
            raise HTTPException(409, '提交订单不属于固定选择批次')

    from datetime import datetime as dt
    results = []
    for oid in ids:
        if snapshot:
            # Serialize validation and mutation against concurrent SQLite writers.
            session.rollback()
            await run_in_threadpool(session.execute, sql_text('BEGIN IMMEDIATE'))
        order = session.query(Order).filter(Order.id == oid).first()
        if not order:
            results.append({"order_id": oid, "success": False, "message": "订单不存在"})
            continue
        if snapshot and order_selection.version(order) != versions[oid]:
            session.rollback()
            results.append({'order_id': oid, 'order_number': order.order_number,
                            'success': False, 'message': '订单自选择后已变更，请核对后重新选择'})
            continue
        if status == "cancelled":
            if order.status == "cancelled":
                results.append({
                    "order_id": oid,
                    "order_number": order.order_number,
                    "success": True,
                    "skipped": True,
                    "message": "订单已取消",
                })
                continue
            order_number = order.order_number
            session.rollback()
            success, result = db.cancel_order(order_id=oid, reason=reason or "后台批量取消", allow_shipped=True)
            session.expire_all()
            if not success:
                results.append({
                    "order_id": oid,
                    "order_number": order_number,
                    "success": False,
                    "message": result,
                })
                continue
            refund = result.get("refund_amount", 0) if isinstance(result, dict) else 0
            results.append({
                "order_id": oid,
                "order_number": result.get("order_number") if isinstance(result, dict) else order_number,

                "success": True,
                "message": f"已取消并回滚库存（退款金额: {refund}）",
                "result": result,
            })
            continue
        if order.status == "cancelled":
            results.append({
                "order_id": oid,
                "order_number": order.order_number,
                "success": False,
                "message": "已取消订单不能再变更状态",
            })
            continue
        if order.status == 'split_pending' and status == 'shipped':
            results.append({
                "order_id": oid,
                "order_number": order.order_number,
                "success": False,
                "message": "分单订单必须先人工确认",
            })
            continue
        if snapshot and status == 'confirmed' and order.status not in ('pending','split_pending','confirmed'):
            session.rollback()
            results.append({'order_id':oid,'order_number':order.order_number,'success':False,
                            'message':'当前订单状态不允许确认，请核对'})
            continue
        if order.status == status:
            results.append({
                "order_id": order.id,
                "order_number": order.order_number,
                "success": True,
                "skipped": True,
                "message": "状态未变化",
            })
            continue
        old_status = order.status
        updated_group = [order]
        order.status = status
        if status == 'confirmed':order.split_confirmed_at = dt.utcnow()
        if status == "shipped" and not getattr(order, "shipped_at", None):
            order.shipped_at = dt.utcnow()
        session.commit()
        session.refresh(order)

        notify = {"sent": False, "detail": None}
        asyncio.create_task(_sync_split_notices())
        try:
            user = order.user
            chat_id = user.telegram_id if user else None
            if status == "shipped" and old_status != "shipped":
                from account_notifications import order_status_notice
                ok, detail = await order_status_notice(db,session,order,'shipped',build_shipping_notify_text(order, session),send_telegram_message_async)
                notify = {"sent": ok, "detail": detail, "type": "shipped"}
            elif status == "confirmed" and old_status != "confirmed":
                from account_notifications import order_status_notice
                ok, detail = await order_status_notice(db,session,order,'confirmed',build_confirm_notify_text(order),send_telegram_message_async)
                notify = {"sent": ok, "detail": detail, "type": "confirmed"}
        except Exception as e:
            notify = {"sent": False, "detail": str(e)}
        results.append({
            "order_id": order.id,
            "order_number": order.order_number,
            "success": True,
            "message": (
                f"整组 {len(updated_group)} 笔分单已确认"
                if len(updated_group) > 1 and status == 'confirmed'
                else f"已更新为 {status}"
            ),
            "updated_order_ids": [item.id for item in updated_group],
            "notify": notify,
        })
    ok_count = sum(1 for item in results if item.get("success"))
    if snapshot:
        session.rollback()
        logger.info('ORDER_SELECTION_CONFIRM batch=%s ids=%s success_ids=%s',
                    snapshot['batch'], ids, [r['order_id'] for r in results if r['success']])
    return {
        "success": ok_count == len(results),
        "status": status,
        "updated": ok_count,
        "total": len(results),
        "results": results,
        "message": f"批量处理完成：{ok_count}/{len(results)} 成功",
    }


def _allocation_metadata(session,order_ids):
    if not order_ids or getattr(session,'bind',None) is None or not inspect_database(session.bind).has_table('order_allocations'):
        return {}
    statement=sql_text('SELECT * FROM order_allocations WHERE order_id IN :ids').bindparams(bindparam('ids',expanding=True))
    result={}
    for row in session.execute(statement,{'ids':order_ids}).mappings():
        result[row['order_id']]=dict(warehouse_id=row['warehouse_id'],
            allocation_mode=row['allocation_mode'],audit=json.loads(row['audit_json']))
    # These are persisted assignment-time facts, not recomputed current ranks.
    if result:
        saved_orders=session.query(Order).filter(Order.id.in_(result)).all()
        warehouse_ids={meta['warehouse_id'] for meta in result.values() if meta.get('warehouse_id') is not None}
        names={w.id:w.name for w in session.query(Warehouse).filter(Warehouse.id.in_(warehouse_ids)).all()} if warehouse_ids else {}
        for saved in saved_orders:
            meta=result[saved.id]
            meta['assigned_at']=saved.created_at.isoformat()+'Z' if saved.created_at else None
            meta['warehouse_name']=names.get(meta['warehouse_id'],'')
            meta['candidate_summary']=[dict(candidate,warehouse_name=names.get(candidate['warehouse_id'],''))
                for candidate in meta['audit'].get('candidates',[])]
    return result


def _split_progress_metadata(session, orders):
    groups={getattr(order, 'split_group_id', None) for order in orders or []}
    groups.discard(None)
    if not groups:
        return {}
    rows=session.query(Order).filter(Order.split_group_id.in_(groups)).all()
    grouped={group: [] for group in groups}
    for row in rows:
        grouped.setdefault(row.split_group_id, []).append(row)
    from split_management import summary
    return {group: summary(rows) for group, rows in grouped.items()}


def _cancellation_fact_metadata(session, order_ids):
    if not order_ids or getattr(session, 'bind', None) is None:
        return {}
    try:
        if not inspect_database(session.bind).has_table('order_cancellation_facts'):
            return {}
        statement=sql_text('''SELECT order_id, reason, cancelled_at
            FROM order_cancellation_facts WHERE order_id IN :ids
            ORDER BY cancelled_at DESC, event_id DESC''').bindparams(bindparam('ids', expanding=True))
        result={}
        for row in session.execute(statement, {'ids': list(order_ids)}).mappings():
            result.setdefault(row['order_id'], {
                'cancellation_reason': row['reason'],
                'cancelled_at': row['cancelled_at'],
            })
        return result
    except Exception:
        return {}


def _bundle_sales_metadata(session, order_ids):
    if not order_ids or getattr(session, 'bind', None) is None:
        return {}
    try:
        if not inspect_database(session.bind).has_table('bundle_sales'):
            return {}
        import bundles
        result={order_id: [] for order_id in order_ids}
        for sale in bundles.receipt_sales(session, list(order_ids)):
            result.setdefault(sale.get('order_id'), []).append(sale)
        return result
    except Exception:
        return {}


def _prime_fhd_waybill_cache(session: Session, orders) -> None:
    """Load successful waybills only for the orders currently rendered."""
    order_ids={
        getattr(order, 'id', None)
        for order in (orders or [])
        if getattr(order, 'id', None) is not None and (
            _waybill_values(getattr(order, 'fhd_waybill_code', None))
            or getattr(order, 'fhd_push_status', None) in {'success', 'partial'}
        )
    }
    cache=getattr(session, '_fhd_waybill_cache', None)
    if cache is None:
        cache={}
        session._fhd_waybill_cache=cache
    loaded_ids=getattr(session, '_fhd_waybill_cache_order_ids', None)
    if loaded_ids is None:
        loaded_ids=set()
        session._fhd_waybill_cache_order_ids=loaded_ids
    missing_ids=order_ids-loaded_ids
    if not missing_ids:
        return
    try:
        rows=(session.query(FenghuodiPushLog.order_id, FenghuodiPushLog.waybill_code)
              .filter(FenghuodiPushLog.status == 'success', FenghuodiPushLog.order_id.in_(missing_ids))
              .order_by(FenghuodiPushLog.id.asc()).all())
        for row in rows:
            order_id=getattr(row, 'order_id', row[0] if isinstance(row, (tuple, list)) else None)
            waybill_code=getattr(row, 'waybill_code', row[1] if isinstance(row, (tuple, list)) else None)
            if order_id is not None:
                cache.setdefault(order_id, []).extend(_waybill_values(waybill_code))
        loaded_ids.update(missing_ids)
    except Exception:
        return


def _receipt_delivery_metadata(session,orders):
    """Admin read projection. No sender, recovery trigger or business writer."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    from success_receipt_audit import records_for_orders,delivery_summary
    if not orders:return {}
    try:
        attempts=records_for_orders([o.order_number for o in orders])
        pairs={(str(o.source_chat_id),o.source_message_id) for o in orders if o.source_chat_id and o.source_message_id}
        outbox={p:[] for p in pairs}
        if pairs:
            clauses=[];params={}
            for i,(chat,message) in enumerate(sorted(pairs)):
                clauses.append(f'(chat_id=:c{i} AND source_message_id=:m{i})')
                params.update({f'c{i}':chat,f'm{i}':message})
            for row in session.execute(text('SELECT chat_id,source_message_id,status FROM telegram_execution_receipts WHERE '+' OR '.join(clauses)),params).mappings():
                outbox[(str(row['chat_id']),row['source_message_id'])].append(dict(row))
        return {o.id:dict(status=delivery_summary(attempts.get(o.order_number,[]),outbox.get((str(o.source_chat_id),o.source_message_id),[])),
                          order_created=True) if o.source_chat_id and o.source_message_id else dict(status='NOT_APPLICABLE',order_created=True) for o in orders}
    except (OSError,ValueError,KeyError,TypeError,SQLAlchemyError):
        return {o.id:dict(status='UNAVAILABLE',order_created=True) for o in orders}


@app.get("/api/orders")
@_run_blocking_endpoint
def get_orders(
    user_id: Optional[int] = None,
    source_chat_id: Optional[str] = None,
    source_message_id: Optional[int] = None,
    warehouse_id: Optional[int] = None,
    category_id: Optional[int] = None,
    logical_category_id: Optional[int] = None,
    ids_only: bool = False,
    status: Optional[str] = None,
    view: Optional[str] = None,
    limit: int = 100,
    offset: int = Query(0, ge=0),
    recorded: Optional[bool] = None,
    record_status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    payment_status: Optional[str] = None,
    include_meta: bool = False,
    include_receipt_evidence: bool = False,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """获取订单列表"""
    query = session.query(Order).options(
        selectinload(Order.user),
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent),
        selectinload(Order.items).selectinload(OrderItem.warehouse),
        selectinload(Order.cancellation_requests),
    )

    limit=min(max(int(limit or 100), 1), 200)
    if user_id:
        query = query.filter(Order.user_id == user_id)
    if source_chat_id is not None or source_message_id is not None:
        if user_id is None or source_chat_id is None or source_message_id is None or source_message_id <= 0:
            raise HTTPException(400, '来源查询必须包含账户、群和正数消息编号')
        query = query.filter(Order.source_chat_id == str(source_chat_id),
                             Order.source_message_id == source_message_id)
        from telegram_source_identity import filter_current_source_orders
        query = filter_current_source_orders(query,source_chat_id)
    if warehouse_id is not None:
        query = query.filter(Order.items.any(OrderItem.warehouse_id == warehouse_id))

    if category_id is not None:
        query=query.filter(Order.items.any(OrderItem.product.has(or_(
            Product.parent_id==category_id, Product.id==category_id))))
    if logical_category_id is not None:
        query=__import__('logical_categories').filter_orders(query,session,logical_category_id,warehouse_id)

    search_text=(search or '').strip()
    if search_text:
        pattern=f'%{search_text}%'
        query=query.filter(or_(
            Order.order_number.ilike(pattern),
            Order.business_order_number.ilike(pattern),
            Order.recipient_name.ilike(pattern),
            Order.recipient_phone.ilike(pattern),
            Order.recipient_address.ilike(pattern),
            Order.user.has(or_(
                User.telegram_id.ilike(pattern),
                User.username.ilike(pattern),
                User.telegram_name.ilike(pattern),
                User.display_name.ilike(pattern),
            )),
        ))

    due_amount=func.coalesce(Order.grand_total, 0)-func.coalesce(Order.paid_amount, 0)
    payment_filter=(payment_status or '').strip().lower()
    if payment_filter in {'unpaid', 'partial'}:
        query=query.filter(due_amount > 0.009)
    elif payment_filter == 'paid':
        query=query.filter(due_amount <= 0.009)

    business_view = (view or '').strip().lower()
    if business_view == 'split':
        from split_management import recording_success_expression
        query = query.filter(Order.status == 'split_pending', ~recording_success_expression())
    elif business_view == 'pending':
        query = query.filter(
            Order.status == 'pending',
            Order.recorded_at.is_(None),
            or_(Order.fhd_push_count.is_(None), Order.fhd_push_count <= 0),
            Order.fhd_push_status.notin_(['success', 'partial']),
        )
    elif business_view == 'recorded':
        from split_management import recording_success_expression
        query = query.filter(
            Order.status.notin_(['shipped', 'cancelled']),
            or_(and_(Order.split_group_id.is_not(None), recording_success_expression()),
                and_(Order.split_group_id.is_(None), or_(Order.recorded_at.is_not(None),
                    Order.status.in_(['confirmed', 'recorded']), Order.fhd_push_count > 0,
                    Order.fhd_push_status.in_(['success', 'partial'])))),
        )
    elif business_view == 'shipped':
        query = query.filter(Order.status == 'shipped')
    elif business_view == 'cancelled':
        query = query.filter(Order.status == 'cancelled')
    elif status:
        query = query.filter(Order.status == status)

    # Recording is now a durable export fact; historical confirmed/logistics
    # states remain a read-only compatibility fallback.
    start_value = start_date or date_from
    end_value = end_date or date_to
    start_dt = _parse_order_date(start_value, "开始")
    end_dt = _parse_order_date(end_value, "结束", end=True)
    date_column = Order.created_at
    if business_view == 'pending':
        from split_management import pending_date_expression
        date_column = pending_date_expression()
    if start_dt is not None:
        query = query.filter(date_column >= start_dt)
    if start_dt is not None and end_dt is not None and start_dt>=end_dt:
        raise HTTPException(400,'开始时间不能晚于结束时间')
    if end_dt is not None:
        query = query.filter(date_column < end_dt)
    record_filter = (record_status or "").strip().lower()
    if recorded is not None:
        record_filter = "recorded" if recorded else "unrecorded"
    if record_filter in {"recorded", "success", "partial", "已录单"}:
        query = query.filter(or_(
            Order.recorded_at.is_not(None),
            Order.status.in_(['confirmed', 'recorded', 'shipped']),
            Order.fhd_push_count > 0,
            Order.fhd_push_status.in_(["success", "partial"]),
        ))
    elif record_filter in {"unrecorded", "not_recorded", "pending", "failed", "未录单"}:
        query = query.filter(
            Order.recorded_at.is_(None),
            Order.status.notin_(['confirmed', 'recorded', 'shipped']),
            or_(Order.fhd_push_count.is_(None), Order.fhd_push_count <= 0),
            Order.fhd_push_status.notin_(["success", "partial"]),
        )

    if business_view == 'cancelled' or (not business_view and status == "cancelled"):
        query = query.order_by(Order.updated_at.desc(), Order.id.desc())
    else:
        query = query.order_by(Order.created_at.desc(), Order.id.desc())
    if ids_only:
        rows=query.all()
        import order_selection
        try: token=order_selection.encode(rows, SESSION_SECRET_KEY)
        except ValueError as exc: raise HTTPException(400, str(exc))
        ids=[r.id for r in rows]
        logger.info('ORDER_SELECTION_CREATED batch=%s ids=%s',
                    order_selection.decode(token,SESSION_SECRET_KEY)['batch'],ids)
        return {'ids':ids,'total':len(ids),'selection_token':token}
    # Keep the historical list response while allowing the admin UI to walk
    # through more than one page without loading an unbounded result set.
    total=query.order_by(None).count() if include_meta else None
    orders = query.offset(offset).limit(limit).all()
    __import__('product_units').prefetch(session,[item for order in orders for item in order.items])
    order_ids=[o.id for o in orders]
    allocations=_allocation_metadata(session, order_ids)
    split_progress=_split_progress_metadata(session, orders)
    cancellation_facts=_cancellation_fact_metadata(session, order_ids)
    bundle_sales=_bundle_sales_metadata(session, order_ids)
    _prime_fhd_waybill_cache(session, orders)

    result=[{
        'allocation':allocations.get(order.id),
        'bundle_sales': bundle_sales.get(order.id, []),
        "id": order.id,
        "order_number": _public_order_number(order),
        "internal_order_number": order.order_number,
        "user_id": order.user_id,
        "user_telegram_id": order.user.telegram_id if order.user else None,
        "user_username": order.user.username if order.user else None,
        "user_telegram_name": getattr(order.user, "telegram_name", None) if order.user else None,
        "user_display_name": getattr(order.user, "display_name", None) if order.user else None,
        "total_amount": order.total_amount,
        "total_shipping_fee": order.total_shipping_fee,
        "grand_total": order.grand_total,
        **__import__('inventory_costs').order_summary(session,order),
        "paid_amount": getattr(order, "paid_amount", 0) or 0,
        "payment_status": getattr(order, "payment_status", "unpaid"),
        "due_amount": max(0, (order.grand_total or 0) - (getattr(order, "paid_amount", 0) or 0)),
        "status": ('confirmed' if order.status == 'split_pending' and _order_is_recorded(order) else order.status),
        "stored_status": order.status,
        "split_group_id": getattr(order, "split_group_id", None),
        "split_index": getattr(order, "split_index", None),
        "split_count": int(getattr(order, "split_count", 1) or 1),
        "split_progress": split_progress.get(getattr(order, 'split_group_id', None)),
        "split_confirmed_at": (
            order.split_confirmed_at.isoformat()
            if getattr(order, "split_confirmed_at", None) else None
        ),
        "cancel_request": (lambda req: {
            "id": req.id,
            "status": req.status,
            "reason": req.reason,
            "created_at": req.created_at,
            "admin_note": req.admin_note,
        } if req else None)(_latest_cancel_request(order)),
        "recipient_name": order.recipient_name,
        "recipient_phone": order.recipient_phone,
        "recipient_address": order.recipient_address,
        "notes": order.notes,
        **cancellation_facts.get(order.id, {}),
        "source_chat_id": getattr(order, "source_chat_id", None),
        "source_message_id": getattr(order, "source_message_id", None),
        "source_chat_type": getattr(order, "source_chat_type", None),
        "source_chat_title": getattr(order, "source_chat_title", None),
        "fhd_push_status": getattr(order, "fhd_push_status", "not_pushed") or "not_pushed",
        "recorded": _order_is_recorded(order),
        "record_status": "recorded" if _order_is_recorded(order) else "unrecorded",
        "recorded_at": business_format(order.recorded_at) if getattr(order, "recorded_at", None) else None,
        "recorded_export_batch_id": getattr(order, "recorded_export_batch_id", None),
        "fhd_push_mode": getattr(order, "fhd_push_mode", None),
        "fhd_push_count": getattr(order, "fhd_push_count", 0) or 0,
        "fhd_last_push_at": order.fhd_last_push_at.isoformat() if getattr(order, "fhd_last_push_at", None) else None,
        "fhd_last_error": getattr(order, "fhd_last_error", None),
        "fhd_trade_id": getattr(order, "fhd_trade_id", None),
         "fhd_waybill_code": getattr(order, "fhd_waybill_code", None),
         "fhd_waybill_codes": _fhd_waybill_codes(order, session),
        "fhd_cp_code": getattr(order, "fhd_cp_code", None),
        "fhd_cp_name": getattr(order, "fhd_cp_name", None),
        "items": [{
            'id': item.id,
            "product_id": item.product_id,
            "product_name": item.product.name if item.product else str(item.product_id),
            "category_name": item.product.parent.name if item.product and item.product.parent else "默认分类",
            "warehouse_id": item.warehouse_id,
            "warehouse_name": item.warehouse.name if item.warehouse else None,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "total_price": item.quantity * item.unit_price,
            "unit": __import__('product_units').historical(session,item),
            **__import__('inventory_costs').item_summary(session,item)
        } for item in order.items],
        "grouped_items": _group_order_items(order.items),
        "created_at": order.created_at,
        "updated_at": order.updated_at
     } for order in orders]
    if include_receipt_evidence:
        receipt_metadata=_receipt_delivery_metadata(session,orders)
        for row in result:row['receipt']=receipt_metadata[row['id']]
    if include_meta:
        return {
            'orders': result,
            'meta': {
                'total': total or 0,
                'limit': limit,
                'offset': offset,
                'has_more': offset + len(result) < (total or 0),
            },
        }
    return result


@app.post('/api/orders/cancellation/check')
@_run_blocking_endpoint
def check_order_cancellation_results(order_ids: str = Form(...),
        session: Session = Depends(get_db_session),
        _auth: str = Depends(require_admin_session)):
    """Read statuses only. Never cancel, refund, or expand the selected IDs."""
    ids=_parse_order_ids(order_ids)
    if not 0<len(ids)<=200:
        raise HTTPException(400,'请选择1至200笔原批次订单进行核对')
    rows=session.query(Order).filter(Order.id.in_(ids)).all()
    by_id={row.id:row for row in rows}
    return {'orders':[{'order_id':oid,'status':by_id[oid].status if oid in by_id else 'missing'} for oid in ids]}


@app.post('/api/orders/selection')
@_run_blocking_endpoint
def freeze_order_selection(order_ids: str = Form(...),
                           session: Session = Depends(get_db_session),
                           _auth: str = Depends(require_admin_or_api_secret)):
    import order_selection
    ids=_parse_order_ids(order_ids)
    if len(ids)!=len(set(ids)) or not 0<len(ids)<=5000:
        raise HTTPException(400, '请选择1至5000笔不同订单')
    rows=session.query(Order).filter(Order.id.in_(ids)).all()
    by_id={row.id:row for row in rows}
    if set(by_id)!=set(ids): raise HTTPException(409,'选择包含不存在的订单，请重新核对')
    token=order_selection.encode([by_id[i] for i in ids],SESSION_SECRET_KEY)
    logger.info('ORDER_SELECTION_CREATED batch=%s ids=%s',
                order_selection.decode(token,SESSION_SECRET_KEY)['batch'],ids)
    return {'ids':ids,'selection_token':token}


@app.post('/api/orders/selection/export')
async def export_order_selection(selection_token: str = Form(...),
                                 preflight: bool = Form(False),
                                 confirm_duplicate_split: bool = Form(False),
                                 session: Session = Depends(get_db_session),
                                 _auth: str = Depends(require_admin_or_api_secret)):
    return await export_orders(selection_token=selection_token, preflight=preflight,
                               confirm_duplicate_split=confirm_duplicate_split,
                               session=session,_auth=_auth)


@app.get("/api/orders/export")
@_run_blocking_endpoint
def export_orders(
    warehouse_id: Optional[int] = None,
    category_id: Optional[int] = None,
    logical_category_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    recorded: Optional[bool] = None,
    record_status: Optional[str] = None,
    status: Optional[str] = None,
    view: Optional[str] = None,
    search: Optional[str] = None,
    payment_status: Optional[str] = None,
    preflight: bool = False,
    confirm_duplicate_split: bool = False,
    file_format: str = 'csv',
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
    selection_token: Optional[str] = None,
):
    """Export order management rows with one column per customer field."""
    import csv
    import io

    start_dt = _parse_order_date(start_date or date_from, "开始")
    end_dt = _parse_order_date(end_date or date_to, "结束", end=True)
    if start_dt is None and end_dt is None:
        # An omitted range means all orders, matching the list screen.
        start_dt = end_dt = None
    if start_dt is not None and end_dt is not None and end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")

    query = session.query(Order).options(
        selectinload(Order.user),
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent),
    )
    search_text=(search or '').strip()
    if search_text:
        pattern=f'%{search_text}%'
        query=query.filter(or_(
            Order.order_number.ilike(pattern),
            Order.business_order_number.ilike(pattern),
            Order.recipient_name.ilike(pattern),
            Order.recipient_phone.ilike(pattern),
            Order.recipient_address.ilike(pattern),
            Order.user.has(or_(
                User.telegram_id.ilike(pattern),
                User.username.ilike(pattern),
                User.telegram_name.ilike(pattern),
                User.display_name.ilike(pattern),
            )),
        ))
    due_amount=func.coalesce(Order.grand_total, 0)-func.coalesce(Order.paid_amount, 0)
    payment_filter=(payment_status or '').strip().lower()
    if payment_filter in {'unpaid', 'partial'}:
        query=query.filter(due_amount > 0.009)
    elif payment_filter == 'paid':
        query=query.filter(due_amount <= 0.009)
    business_view = (view or '').strip().lower()
    if business_view == 'split':
        from split_management import recording_success_expression
        query = query.filter(Order.status == 'split_pending', ~recording_success_expression())
    elif business_view == 'pending':
        query = query.filter(
            Order.status == 'pending',
            Order.recorded_at.is_(None),
            or_(Order.fhd_push_count.is_(None), Order.fhd_push_count <= 0),
            Order.fhd_push_status.notin_(['success', 'partial']),
        )
    elif business_view == 'recorded':
        from split_management import recording_success_expression
        query = query.filter(
            Order.status.notin_(['shipped', 'cancelled']),
            or_(and_(Order.split_group_id.is_not(None), recording_success_expression()),
                and_(Order.split_group_id.is_(None), or_(Order.recorded_at.is_not(None),
                    Order.status.in_(['confirmed', 'recorded']), Order.fhd_push_count > 0,
                    Order.fhd_push_status.in_(['success', 'partial'])))),
        )
    elif business_view == 'shipped':
        query = query.filter(Order.status == 'shipped')
    elif business_view == 'cancelled':
        query = query.filter(Order.status == 'cancelled')
    elif status:
        query = query.filter(Order.status == status)
    date_column = Order.created_at
    if business_view == 'pending':
        from split_management import pending_date_expression
        date_column = pending_date_expression()
    if start_dt is not None:
        query = query.filter(date_column >= start_dt)
    if end_dt is not None:
        query = query.filter(date_column < end_dt)
    record_filter = (record_status or "").strip().lower()
    if recorded is not None:
        record_filter = "recorded" if recorded else "unrecorded"
    if record_filter in {"recorded", "success", "partial", "已录单"}:
        query = query.filter(or_(Order.recorded_at.is_not(None), Order.status.in_(['confirmed', 'recorded', 'shipped']), Order.fhd_push_count > 0, Order.fhd_push_status.in_(["success", "partial"])))
    elif record_filter in {"unrecorded", "not_recorded", "pending", "failed", "未录单"}:
        query = query.filter(
            Order.recorded_at.is_(None),
            Order.status.notin_(['confirmed', 'recorded', 'shipped']),
            or_(Order.fhd_push_count.is_(None), Order.fhd_push_count <= 0),
            Order.fhd_push_status.notin_(["success", "partial"]),
        )
    if warehouse_id is not None:
        query=query.filter(Order.items.any(OrderItem.warehouse_id==warehouse_id))
    if category_id is not None:
        query=query.filter(Order.items.any(OrderItem.product.has(or_(
            Product.parent_id==category_id, Product.id==category_id))))
    if logical_category_id is not None:
        query=__import__('logical_categories').filter_orders(query,session,logical_category_id,warehouse_id)
    selected = None
    changed_ids = []
    if selection_token:
        import order_selection
        try: selected=order_selection.decode(selection_token, SESSION_SECRET_KEY)
        except ValueError as exc: raise HTTPException(400, str(exc))
        ids=[row[0] for row in selected['orders']]
        orders=session.query(Order).options(selectinload(Order.items)).filter(Order.id.in_(ids)).all()
        by_id={o.id:o for o in orders}
        missing=[i for i in ids if i not in by_id]
        if missing: raise HTTPException(409, {'message':'固定批次包含已删除订单，未生成不完整导出', 'missing_ids':missing})
        orders=[by_id[i] for i in ids]
        changed_ids=[i for i,v in selected['orders'] if order_selection.version(by_id[i])!=v]
    else:
        orders = query.order_by(Order.created_at.desc(), Order.id.desc()).all()
    conflicts = _split_export_conflicts(orders)
    if preflight:
        return {
            'success': True,
            'order_count': len(orders),
            'requires_confirmation': bool(conflicts),
            'conflicts': conflicts,
            'selected_ids': [o.id for o in orders],
            'changed_ids': changed_ids,
        }
    if conflicts and not confirm_duplicate_split:
        raise HTTPException(status_code=409, detail={
            'code': 'SPLIT_ADDRESS_DUPLICATE',
            'message': '同一地址同一天包含多笔分单，默认只能导出一笔。请人工确认后再继续导出。',
            'conflicts': conflicts,
        })

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    export_rows=[["订单号", "姓名", "手机号", "地址", "货品", "订单状态", "录单状态", "下单时间"]]
    matched_category_id = logical_category_id if logical_category_id is not None else category_id
    export_rows[0].append('实际配货明细（数量及单位）')
    if matched_category_id is not None:
        export_rows[0].append('命中分类（整单导出）')
    bundle_sales = _bundle_sales_metadata(session, [order.id for order in orders])
    writer.writerow(export_rows[0])
    for order in orders:
        products = [group['text'] for group in _group_order_items(order.items)]
        from order_export import item_text, sales_text
        sales = bundle_sales.get(order.id, [])
        linked_ids = {item_id for sale in sales for item_id in sale.get('item_ids', [])}
        ordinary = [item for item in order.items if item.id not in linked_ids]
        export_row=[
            _public_order_number(order),
            order.recipient_name or "",
            order.recipient_phone or "",
            order.recipient_address or "",
            sales_text(sales, _group_order_items(ordinary), ordinary) if sales else item_text(_group_order_items(order.items),order.items),
            _order_status_label(order.status),
            # A file is returned only after the whole batch has been generated
            # and its recording transaction commits.  Therefore every row in
            # the returned file is recorded by this export (or was recorded by
            # an earlier one).
            "已录单",
            business_format(order.created_at),
        ]
        export_row.append(item_text(_group_order_items(order.items), order.items))
        if matched_category_id is not None:
            category=session.get(Product,matched_category_id)
            export_row.append(category.name if category else str(matched_category_id))
        writer.writerow(export_row)
        export_rows.append(export_row)
    stamp = business_now().strftime("%Y%m%d_%H%M%S")
    if selected:
        logger.info('ORDER_SELECTION_EXPORT_GENERATED batch=%s ids=%s changed_ids=%s',
                    selected['batch'], [o.id for o in orders], changed_ids)
    if file_format not in {'csv', 'xlsx'}:
        raise HTTPException(400, '导出格式仅支持CSV或XLSX')
    try:
        if file_format == 'xlsx':
            from order_export import xlsx
            content = xlsx(export_rows)
            media_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        else:
            content = output.getvalue().encode('utf-8')
            media_type = "text/csv; charset=utf-8"
    except Exception:
        # Record a bounded, non-sensitive failure fact in a fresh transaction.
        # Never add membership rows or recording markers for a file that was
        # not generated successfully.
        session.rollback()
        failed_at = datetime.utcnow()
        failed_batch_id = uuid.uuid4().hex
        session.add(OrderExportBatch(
            id=failed_batch_id,
            selection_batch=(selected or {}).get('batch'),
            file_format=file_format,
            result='failed',
            order_count=0,
            duplicate_count=0,
            error_code='EXPORT_GENERATION_FAILED',
            created_at=failed_at,
            completed_at=failed_at,
        ))
        session.commit()
        logger.exception('ORDER_EXPORT_GENERATION_FAILED batch=%s format=%s', failed_batch_id, file_format)
        raise HTTPException(500, '导出文件生成失败，未改变任何订单的录单状态')

    # Generate first; only then atomically commit the batch, membership and
    # first-recorded markers. A generation or database failure returns no file.
    if any(order.split_group_id for order in orders):
        try:
            connection = session.connection()
            if connection.dialect.name == 'sqlite':
                # SELECTs alone do not begin a SQLite DBAPI transaction. Take
                # the bounded normal writer lock before refreshing success
                # facts, so concurrent exports cannot both count as first.
                driver = connection.connection.driver_connection
                if not driver.in_transaction:
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
            for order in sorted(orders, key=lambda row: row.id):
                session.refresh(order, with_for_update=True)
                if order.split_group_id and order.status == 'cancelled':
                    session.rollback()
                    raise HTTPException(409, '分单已取消，未改变录单状态')
        except OperationalError:
            session.rollback()
            raise HTTPException(409, '录单状态正在更新，请核对后重试；本次未返回文件')
    completed_at = datetime.utcnow()
    export_batch_id = uuid.uuid4().hex
    duplicate_count = sum(1 for order in orders if _order_is_recorded(order))
    session.add(OrderExportBatch(
        id=export_batch_id,
        selection_batch=(selected or {}).get('batch'),
        file_format=file_format,
        result='success',
        order_count=len(orders),
        duplicate_count=duplicate_count,
        content_sha256=hashlib.sha256(content).hexdigest(),
        created_at=completed_at,
        completed_at=completed_at,
    ))
    for order in orders:
        was_duplicate = _order_is_recorded(order)
        session.add(OrderExportItem(
            batch_id=export_batch_id,
            order_id=order.id,
            order_number=_public_order_number(order),
            was_duplicate=was_duplicate,
            created_at=completed_at,
        ))
        if not was_duplicate:
            order.recorded_at = completed_at
            order.recorded_export_batch_id = export_batch_id
            session.add(order)
        # Reconcile both first exports and legacy duplicate exports atomically
        # with their successful export audit. Ordinary order semantics stay put.
        if order.status == 'split_pending':
            order.status = 'confirmed'
            session.add(order)
    session.commit()
    suffix = 'xlsx' if file_format == 'xlsx' else 'csv'
    return Response(content=content, media_type=media_type, headers={
        'Content-Disposition': f'attachment; filename="orders_{stamp}.{suffix}"',
        'X-Order-Export-Batch': export_batch_id,
        'X-Order-Export-Count': str(len(orders)),
    })


@app.get('/api/orders/export-reconciliation')
@_run_blocking_endpoint
def order_export_reconciliation(
    day: Optional[str] = None,
    warehouse_id: Optional[int] = None,
    logical_category_id: Optional[int] = None,
    view: Optional[str] = None,
    search: Optional[str] = None,
    payment_status: Optional[str] = None,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """Read-only daily facts with explicit batch/order and filter semantics."""
    from order_reconciliation import metrics
    try:
        return metrics(session,day,warehouse_id=warehouse_id,logical_category_id=logical_category_id,
                       view=view,search=search,payment_status=payment_status)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from None


@app.get("/api/orders/cancelled")
@_run_blocking_endpoint
def list_cancelled_orders(
    limit: int = 200,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """List cancelled orders still in database."""
    rows = (
        session.query(Order)
        .options(
            selectinload(Order.user),
            selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent),
            selectinload(Order.items).selectinload(OrderItem.warehouse),
        )
        .filter(Order.status == "cancelled")
        .order_by(Order.updated_at.desc(), Order.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return {
        "success": True,
        "total": len(rows),
        "orders": [
            {
                "id": order.id,
                "order_number": order.order_number,
                "user_id": order.user_id,
                "user_username": order.user.username if order.user else None,
                "user_telegram_id": order.user.telegram_id if order.user else None,
                "recipient_name": order.recipient_name,
                "recipient_phone": order.recipient_phone,
                "recipient_address": order.recipient_address,
                "grand_total": order.grand_total,
                "paid_amount": getattr(order, "paid_amount", 0) or 0,
                "status": order.status,
                "notes": order.notes,
                **__import__('cancellation_facts').latest(order),
                "created_at": order.created_at,
                "updated_at": order.updated_at,
                "items": [
                    {
                        "product_id": item.product_id,
                        "product_name": item.product.name if item.product else "",
                        "parent_name": item.product.parent.name if item.product and item.product.parent else "",
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "unit": __import__('product_units').historical(session,item),
                    }
                    for item in (order.items or [])
                ],
            }
            for order in rows
        ],
    }


@app.get("/api/orders/deleted_traces")
@_run_blocking_endpoint
def list_deleted_order_traces(
    limit: int = 100,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """List hard-deleted orders that still have cancel/out stock traces."""
    existing = {row[0] for row in session.query(Order.order_number).all() if row[0]}
    q = (
        session.query(StockMovement)
        # 不能按 ref_id 前缀筛：.87 的订单ID 已改成 A1/H2 这种业务号，
        # like('ORD%') 会把新格式的删除痕迹全漏掉。movement_type 已经足够限定。
        .filter(StockMovement.ref_id.isnot(None))
        .filter(StockMovement.movement_type.in_(["cancel_in", "order_out"]))
        .order_by(StockMovement.id.desc())
    )
    grouped = {}
    for move in q.all():
        ref = move.ref_id
        if not ref or ref in existing:
            continue
        item = grouped.get(ref)
        if not item:
            item = {
                "order_number": ref,
                "deleted": True,
                "last_at": move.created_at,
                "first_at": move.created_at,
                "total_qty": 0,
                "items": {},
            }
            grouped[ref] = item
        if move.created_at and (not item["last_at"] or move.created_at > item["last_at"]):
            item["last_at"] = move.created_at
        if move.created_at and (not item["first_at"] or move.created_at < item["first_at"]):
            item["first_at"] = move.created_at
        if move.movement_type == "cancel_in":
            qty = int(move.change_qty or 0)
            product = session.query(Product).filter(Product.id == move.product_id).first()
            name = product.name if product else str(move.product_id)
            parent = product.parent.name if product and getattr(product, "parent", None) else ""
            key = f"{move.product_id}"
            bucket = item["items"].setdefault(key, {"product_id": move.product_id, "product_name": name, "parent_name": parent, "quantity": 0})
            bucket["quantity"] += qty
            item["total_qty"] += qty
    traces = []
    for item in grouped.values():
        traces.append({
            "order_number": item["order_number"],
            "deleted": True,
            "first_at": item["first_at"],
            "last_at": item["last_at"],
            "total_qty": item["total_qty"],
            "items": list(item["items"].values()),
        })
    traces.sort(key=lambda row: str(row.get("last_at") or ""), reverse=True)
    return {"success": True, "total": len(traces), "traces": traces[: min(max(limit, 1), 300)]}


@app.get("/api/orders/address_usage")
@_run_blocking_endpoint
def get_address_daily_usage(
    address: str,
    exclude_order_id: Optional[int] = None,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """某个收货地址今天已下多少支、还能下多少 —— 给 Bot 下单预览用。"""
    limits = _packaging_limits()
    usage = _address_daily_units(session, address, exclude_order_id=exclude_order_id)
    cap = int(limits.get('address_daily_max_units') or 0)
    usage.update({
        'limit': cap,
        'remaining': max(0, cap - usage['used_units']) if cap else None,
        'limits': limits,
    })
    return usage


@app.get("/api/orders/{order_id}")
@_run_blocking_endpoint
def get_order_by_id(
    order_id: int,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """获取单个订单详情"""
    order = session.query(Order).options(
        selectinload(Order.user),
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent),
        selectinload(Order.items).selectinload(OrderItem.warehouse),
    ).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 按仓库分组商品
    items_by_warehouse = {}
    for item in order.items:
        warehouse_name = item.warehouse.name if item.warehouse else "默认仓库"
        if warehouse_name not in items_by_warehouse:
            items_by_warehouse[warehouse_name] = []
        items_by_warehouse[warehouse_name].append({
            "product_name": item.product.name if item.product else str(item.product_id),
            "category_name": item.product.parent.name if item.product and item.product.parent else "默认分类",
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "total_price": item.quantity * item.unit_price,
            "unit": __import__('product_units').historical(session,item),
            **__import__('inventory_costs').item_summary(session,item)
        })

    return {
        "id": order.id,
        "order_number": order.order_number,
        "allocation": _allocation_metadata(session, [order.id]).get(order.id),
        "user_id": order.user_id,
        "user_telegram_id": order.user.telegram_id if order.user else None,
        "user_username": order.user.username if order.user else None,
        "user_telegram_name": getattr(order.user, "telegram_name", None) if order.user else None,
        "user_display_name": getattr(order.user, "display_name", None) if order.user else None,
        "total_amount": order.total_amount,
        "total_shipping_fee": order.total_shipping_fee,
        "grand_total": order.grand_total,
        **__import__('inventory_costs').order_summary(session,order),
        "paid_amount": getattr(order, "paid_amount", 0) or 0,
        "payment_status": getattr(order, "payment_status", "unpaid"),
        "due_amount": max(0, (order.grand_total or 0) - (getattr(order, "paid_amount", 0) or 0)),
        "status": order.status,
        "cancel_request": (lambda req: {
            "id": req.id,
            "status": req.status,
            "reason": req.reason,
            "created_at": req.created_at,
            "admin_note": req.admin_note,
            "reviewed_by": req.reviewed_by,
            "reviewed_at": req.reviewed_at,
        } if req else None)(_latest_cancel_request(order)),
        "recipient_name": order.recipient_name,
        "recipient_phone": order.recipient_phone,
        "recipient_address": order.recipient_address,
        "notes": order.notes,
        **__import__('cancellation_facts').latest(order),
        "source_chat_id": getattr(order, "source_chat_id", None),
        "source_message_id": getattr(order, "source_message_id", None),
        "source_chat_type": getattr(order, "source_chat_type", None),
        "source_chat_title": getattr(order, "source_chat_title", None),
        "fhd_push_status": getattr(order, "fhd_push_status", "not_pushed") or "not_pushed",
        "fhd_push_mode": getattr(order, "fhd_push_mode", None),
        "fhd_push_count": getattr(order, "fhd_push_count", 0) or 0,
        "fhd_last_push_at": order.fhd_last_push_at.isoformat() if getattr(order, "fhd_last_push_at", None) else None,
        "fhd_last_error": getattr(order, "fhd_last_error", None),
        "fhd_trade_id": getattr(order, "fhd_trade_id", None),
         "fhd_waybill_code": getattr(order, "fhd_waybill_code", None),
         "fhd_waybill_codes": _fhd_waybill_codes(order, session),
        "fhd_cp_code": getattr(order, "fhd_cp_code", None),
        "fhd_cp_name": getattr(order, "fhd_cp_name", None),
        "items_by_warehouse": items_by_warehouse,
        "bundle_sales": __import__('bundles').receipt_sales(session,[order.id]) if inspect_database(db.engine).has_table('bundle_sales') else [],
        "items": [{
            "product_id": item.product_id,
            "product_name": item.product.name if item.product else str(item.product_id),
            "category_name": item.product.parent.name if item.product and item.product.parent else "默认分类",
            "warehouse_id": item.warehouse_id,
            "warehouse_name": item.warehouse.name if item.warehouse else None,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "shipping_fee": item.shipping_fee,
            "unit": __import__('product_units').historical(session,item),
            **__import__('inventory_costs').item_summary(session,item),
        } for item in order.items],
        "created_at": order.created_at,
        "updated_at": order.updated_at
    }


@app.post("/api/orders/{order_id}/update_address")
@_run_blocking_endpoint
def update_order_address(
    order_id: int,
    recipient_address: str = Form(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """后台修改未发货订单的收货地址。"""
    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if order.status == "shipped":
        raise HTTPException(status_code=400, detail="订单已发货，收货地址不能再修改")
    if order.status == "cancelled":
        raise HTTPException(status_code=400, detail="已取消订单不能修改收货地址")

    cleaned_address = " ".join(str(recipient_address or "").split())
    if len(cleaned_address) < 4:
        raise HTTPException(status_code=400, detail="收货地址过短，请填写完整详细地址")
    if len(cleaned_address) > 500:
        raise HTTPException(status_code=400, detail="收货地址过长，请控制在500字以内")

    blocked, hit = db.is_address_blocked(cleaned_address)
    if blocked:
        reason = hit.reason if hit and hit.reason else "地址异常"
        raise HTTPException(status_code=403, detail=f"收货地址命中异常地址库，不能修改（{reason}）")

    old_address = str(order.recipient_address or "").strip()
    requires_fhd_resync = bool(
        int(getattr(order, "fhd_push_count", 0) or 0) > 0
        or str(getattr(order, "fhd_push_status", "") or "") in {"success", "partial"}
    )
    if old_address == cleaned_address:
        return {
            "success": True,
            "message": "收货地址未变化",
            "recipient_address": order.recipient_address,
            "fhd_resync_required": requires_fhd_resync,
        }

    order.recipient_address = cleaned_address
    session.commit()
    session.refresh(order)

    message = "收货地址已修改"
    if requires_fhd_resync:
        message += "。该订单已推送风火递，请同步在风火递修改地址后再打单"
    return {
        "success": True,
        "message": message,
        "recipient_address": order.recipient_address,
        "fhd_resync_required": requires_fhd_resync,
    }




@app.post("/api/orders/{order_id}/update_shipping_fee")
@_run_blocking_endpoint
def update_order_shipping_fee(
    order_id: int,
    total_shipping_fee: float = Form(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """后台修改已下单订单的运费，并同步重算订单总价与收款状态。"""
    order = session.query(Order).options(
        selectinload(Order.items),
        selectinload(Order.user),
    ).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if order.status == "cancelled":
        raise HTTPException(status_code=400, detail="已取消订单不能修改运费")

    try:
        new_fee = round(float(total_shipping_fee), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="运费必须是数字")

    if new_fee < 0:
        raise HTTPException(status_code=400, detail="运费不能为负数")
    if new_fee > 100000:
        raise HTTPException(status_code=400, detail="运费过大，请检查输入")

    old_fee = round(float(order.total_shipping_fee or 0), 2)
    old_grand_total = round(float(order.grand_total or 0), 2)
    product_amount = round(float(order.total_amount or 0), 2)
    paid_before = round(float(getattr(order, "paid_amount", 0) or 0), 2)

    if abs(old_fee - new_fee) < 1e-9:
        due_amount = max(0.0, round(old_grand_total - paid_before, 2))
        return {
            "success": True,
            "message": "运费未变化",
            "order_id": order.id,
            "order_number": order.order_number,
            "total_amount": product_amount,
            "total_shipping_fee": old_fee,
            "grand_total": old_grand_total,
            "paid_amount": paid_before,
            "payment_status": getattr(order, "payment_status", "unpaid"),
            "due_amount": due_amount,
            "balance_credit": 0.0,
        }

    new_grand_total = round(product_amount + new_fee, 2)
    order.total_shipping_fee = new_fee
    order.grand_total = new_grand_total

    # 明细运费：总量记在第一项，其余清零，保持与创建订单时的展示习惯一致
    items = list(order.items or [])
    for idx, item in enumerate(items):
        item.shipping_fee = new_fee if idx == 0 else 0.0

    balance_credit = 0.0
    paid_amount = paid_before
    if paid_amount - new_grand_total > 1e-9:
        balance_credit = round(paid_amount - new_grand_total, 2)
        paid_amount = new_grand_total
        if order.user is not None:
            order.user.balance = round(float(order.user.balance or 0) + balance_credit, 2)

    order.paid_amount = paid_amount
    db._refresh_order_payment_status(order)
    session.commit()
    session.refresh(order)

    due_amount = max(0.0, round(float(order.grand_total or 0) - float(order.paid_amount or 0), 2))
    message = f"运费已从 ¥{old_fee:.2f} 修改为 ¥{new_fee:.2f}"
    if balance_credit > 0:
        message += f"，多收 ¥{balance_credit:.2f} 已转入用户预存"
    elif due_amount > 0 and paid_before + 1e-9 >= old_grand_total:
        message += f"，订单新增欠款 ¥{due_amount:.2f}"

    return {
        "success": True,
        "message": message,
        "order_id": order.id,
        "order_number": order.order_number,
        "total_amount": product_amount,
        "total_shipping_fee": round(float(order.total_shipping_fee or 0), 2),
        "grand_total": round(float(order.grand_total or 0), 2),
        "paid_amount": round(float(order.paid_amount or 0), 2),
        "payment_status": getattr(order, "payment_status", "unpaid"),
        "due_amount": due_amount,
        "balance_credit": balance_credit,
        "old_total_shipping_fee": old_fee,
        "old_grand_total": old_grand_total,
    }


@app.post("/api/orders/{order_id}/cancel_request")
@_run_blocking_endpoint
def request_order_cancel(
    order_id: int,
    telegram_id: str = Form(...),
    reason: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """客户提交取消申请；这里只记录申请，绝不改变订单或库存。"""
    raise HTTPException(status_code=403, detail='订单已录入，请联系客服处理取消或修改')
    success, result = db.request_order_cancellation(
        order_id=order_id,
        telegram_id=str(telegram_id).strip(),
        reason=reason,
    )
    if not success:
        raise HTTPException(status_code=400, detail=result)
    return {
        "success": True,
        "message": "取消申请已提交，等待后台审核；审核前订单仍然有效",
        "request": result,
    }


@app.post("/api/orders/cancel_request_by_number")
@_run_blocking_endpoint
def request_order_cancel_by_number(
    order_number: str = Form(...),
    telegram_id: str = Form(...),
    reason: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """按订单号提交客户取消申请，不直接回滚库存。"""
    raise HTTPException(status_code=403, detail='订单已录入，请联系客服处理取消或修改')
    success, result = db.request_order_cancellation(
        order_number=order_number,
        telegram_id=str(telegram_id).strip(),
        reason=reason,
    )
    if not success:
        raise HTTPException(status_code=400, detail=result)
    return {
        "success": True,
        "message": "取消申请已提交，等待后台审核；审核前订单仍然有效",
        "request": result,
    }


@app.get("/api/order-cancellation-requests")
@_run_blocking_endpoint
def list_order_cancellation_requests(
    status: str = "pending",
    limit: int = 100,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """后台查看客户取消申请。"""
    allowed = {"pending", "approved", "rejected", "all"}
    status = (status or "pending").strip().lower()
    if status not in allowed:
        raise HTTPException(status_code=400, detail="无效的取消申请状态")
    query = session.query(OrderCancellationRequest).options(
        selectinload(OrderCancellationRequest.order),
        selectinload(OrderCancellationRequest.user),
    )
    if status != "all":
        query = query.filter(OrderCancellationRequest.status == status)
    rows = query.order_by(
        OrderCancellationRequest.created_at.desc(),
        OrderCancellationRequest.id.desc(),
    ).limit(min(max(int(limit or 1), 1), 500)).all()
    return {
        "success": True,
        "total": len(rows),
        "requests": [{
            "id": item.id,
            "order_id": item.order_id,
            "order_number": item.order_number,
            "telegram_id": item.telegram_id,
            "username": item.user.username if item.user else None,
            "display_name": getattr(item.user, "display_name", None) if item.user else None,
            "recipient_name": item.order.recipient_name if item.order else None,
            "recipient_phone": item.order.recipient_phone if item.order else None,
            "grand_total": item.order.grand_total if item.order else None,
            "order_status": item.order.status if item.order else None,
            "reason": item.reason,
            "status": item.status,
            "admin_note": item.admin_note,
            "reviewed_by": item.reviewed_by,
            "created_at": item.created_at,
            "reviewed_at": item.reviewed_at,
        } for item in rows],
    }


@app.post("/api/order-cancellation-requests/{request_id}/review")
@_run_blocking_endpoint
def review_order_cancellation_request(
    request_id: int,
    decision: str = Form(...),
    admin_note: Optional[str] = Form(None),
    reviewed_by: Optional[str] = Form("后台管理员"),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """后台审核取消申请；批准才会真正取消订单并回滚库存。"""
    reject_api_key_direct_cancel(_auth)
    if decision == 'approve':
        admin_note=require_cancellation_reason(admin_note)
    success, result = db.review_order_cancellation(
        request_id=request_id,
        decision=decision,
        admin_note=admin_note,
        reviewed_by=reviewed_by,
    )
    if not success:
        raise HTTPException(status_code=400, detail=result)
    if str(decision).strip().lower() == "approve":
        message = "取消申请已批准，订单已取消并回滚库存"
    else:
        message = "取消申请已驳回，订单保持原状态"
    return {"success": True, "message": message, "result": result}


@app.post("/api/orders/{order_id}/update_status")
async def update_order_status(
    order_id: int,
    status: str = Form(...),
    reason: Optional[str] = Form(None),
    tracking_number: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """更新订单状态。取消时自动回滚库存并退款。

    发货可以带快递单号（批发端后台手填）。「必填」是前端的事，接口保持宽松，
    这样批量发货等旧调用不会因为没带单号而失败。
    """
    if status not in ["pending", "confirmed", "shipped", "cancelled"]:
        raise HTTPException(status_code=400, detail="无效的状态")

    tracking_number = (tracking_number or "").strip()
    if len(tracking_number) > 100:
        raise HTTPException(status_code=400, detail="快递单号不能超过100个字符")

    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 取消走专用回滚逻辑
    if status == "cancelled":
        reject_api_key_direct_cancel(_auth)
        reason = require_cancellation_reason(reason)
        success, result = db.cancel_order(order_id=order_id, reason=reason or "后台取消", allow_shipped=True)
        if not success:
            raise HTTPException(status_code=400, detail=result)
        refund = result.get('refund_amount', 0) if isinstance(result, dict) else 0
        return {
            "success": True,
            "message": f"订单已取消，已退款并回滚库存（退款金额: {refund}）",
            "result": result,
        }

    # 其他状态直接更新
    if order.status == 'cancelled':
        raise HTTPException(status_code=400, detail="已取消订单不能再变更状态")
    if order.status == 'split_pending' and status == 'shipped':
        raise HTTPException(status_code=400, detail="分单订单必须先人工确认，确认后才能发货")

    old_status = order.status
    old_tracking_number = (getattr(order, 'fhd_waybill_code', None) or '').strip()
    updated_orders = [order]
    order.status = status
    if status == 'confirmed':order.split_confirmed_at = datetime.utcnow()
    if status == 'shipped' and not getattr(order, 'shipped_at', None):
        from datetime import datetime as dt
        order.shipped_at = dt.utcnow()
    if status == 'shipped':
        # 批发端（.87）单号后台手填，允许清空 —— 与波派机器人口径一致。
        # 下单端（.24）单号是风火递推回来的，一次「标记发货」不能把它清掉，
        # 所以只有真的填了新单号才覆盖。
        import config as _cfg_track
        if tracking_number or getattr(_cfg_track, 'MANUAL_TRACKING_ON_SHIP', False):
            order.fhd_waybill_code = tracking_number
    session.commit()
    session.refresh(order)

    notify = {'sent': False, 'detail': None}
    asyncio.create_task(_sync_split_notices())
    try:
        user = order.user
        chat_id = user.telegram_id if user else None
        # 补录/改单号也要通知客户，不然客户拿不到新单号
        if status == 'shipped' and (
            old_status != 'shipped' or tracking_number != old_tracking_number
        ):
            from account_notifications import order_status_notice
            ok, detail = await order_status_notice(db,session,order,'shipped',build_shipping_notify_text(order, session),send_telegram_message_async)
            notify = {'sent': ok, 'detail': detail, 'type': 'shipped'}
        elif status == 'confirmed' and old_status != 'confirmed':
            confirm_text = build_confirm_notify_text(order)
            if len(updated_orders) > 1:
                numbers = '、'.join(f'#{item.order_number}' for item in updated_orders)
                confirm_text += f'\n同组分单已全部确认: {numbers}'
            from account_notifications import order_status_notice
            ok, detail = await order_status_notice(db,session,order,'confirmed',confirm_text,send_telegram_message_async)
            notify = {'sent': ok, 'detail': detail, 'type': 'confirmed'}
    except Exception as e:
        notify = {'sent': False, 'detail': str(e)}

    msg = f"订单状态已更新为: {status}"
    if notify.get('type') == 'shipped':
        msg += ' (已通知客户发货)' if notify.get('sent') else f" (发货通知失败: {notify.get('detail')})"
    elif notify.get('type') == 'confirmed':
        msg += ' (已通知客户确认)' if notify.get('sent') else f" (确认通知失败: {notify.get('detail')})"

    return {
        "success": True,
        "message": msg,
        "notify": notify,
        "order_id": order.id,
        "order_number": order.order_number,
        "updated_order_ids": [item.id for item in updated_orders],
        "updated_order_numbers": [item.order_number for item in updated_orders],
        "tracking_number": getattr(order, "fhd_waybill_code", None),
        "tracking_numbers": _fhd_waybill_codes(order, session),
    }


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order_api(
    order_id: int,
    reason: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """取消订单并回滚库存/余额"""
    reject_api_key_direct_cancel(_auth)
    reason = require_cancellation_reason(reason)
    success, result = db.cancel_order(order_id=order_id, reason=reason or "API取消", allow_shipped=True)
    if not success:
        raise HTTPException(status_code=400, detail=result)
    asyncio.create_task(_sync_split_notices())
    return {"success": True, "message": "订单已取消", "result": result}


@app.post("/api/orders/{order_id}/restore")
@_run_blocking_endpoint
def restore_order_api(
    order_id: int,
    to_status: Optional[str] = Form("pending"),
    reason: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session),
):
    """Restore a cancelled order and re-deduct stock."""
    session.rollback()
    success, result = db.restore_order(
        order_id=order_id,
        to_status=(to_status or "pending").strip() or "pending",
        reason=reason or "后台恢复已取消订单",
    )
    session.expire_all()
    if not success:
        raise HTTPException(status_code=400, detail=result)
    return {"success": True, "message": "订单已恢复", "result": result}


@app.post("/api/orders/batch_restore")
@_run_blocking_endpoint
def batch_restore_orders(
    order_ids: str = Form(...),
    to_status: Optional[str] = Form("pending"),
    reason: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session),
):
    """Batch restore cancelled orders."""
    ids = _parse_order_ids(order_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="请选择订单")
    if len(ids)>200:
        raise HTTPException(400,'单次最多200笔，请由后台逐批处理明确选择的订单')
    results = []
    for oid in ids:
        session.rollback()
        success, result = db.restore_order(
            order_id=oid,
            to_status=(to_status or "pending").strip() or "pending",
            reason=reason or "后台批量恢复",
        )
        session.expire_all()
        if success:
            results.append({
                "order_id": oid,
                "order_number": result.get("order_number") if isinstance(result, dict) else None,
                "success": True,
                "message": "已恢复",
                "result": result,
            })
        else:
            results.append({"order_id": oid, "success": False, "message": result})
    ok_count = sum(1 for item in results if item.get("success"))
    return {
        "success": ok_count == len(results),
        "updated": ok_count,
        "total": len(results),
        "results": results,
        "message": f"批量恢复完成：{ok_count}/{len(results)} 成功",
    }


@app.post("/api/orders/{order_id}/delete")
@_run_blocking_endpoint
def delete_order_api(
    order_id: int,
    reason: Optional[str] = Form(None),
    allow_shipped: bool = Form(True),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """删除订单。未取消订单会先走取消逻辑回滚库存，再硬删除订单记录。"""
    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    rollback_result = None
    if order.status != "cancelled":
        reject_api_key_direct_cancel(_auth)
        reason=require_cancellation_reason(reason)
        # 当前 session 已读到旧订单；先回滚，避免和 cancel_order 内部会话互相影响。
        session.rollback()
        success, rollback_result = db.cancel_order(
            order_id=order_id,
            reason=reason or "后台删除订单前自动取消",
            allow_shipped=bool(allow_shipped),
        )
        if not success:
            raise HTTPException(status_code=400, detail=rollback_result)
        session.expire_all()

    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        return {"success": True, "message": "订单已删除", "order_id": order_id, "rollback": rollback_result}

    order_number = order.order_number
    session.query(Payment).filter(Payment.order_id == order.id).update(
        {Payment.order_id: None},
        synchronize_session=False,
    )
    session.query(FenghuodiPushLog).filter(FenghuodiPushLog.order_id == order.id).update(
        {FenghuodiPushLog.order_id: None},
        synchronize_session=False,
    )
    session.delete(order)
    session.commit()
    return {
        "success": True,
        "message": "订单已删除",
        "order_id": order_id,
        "order_number": order_number,
        "rollback": rollback_result,
    }


@app.post("/api/orders/cancel_by_number")
@_run_blocking_endpoint
def cancel_order_by_number(
    order_number: str = Form(...),
    telegram_id: Optional[str] = Form(None),
    reason: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """按订单号取消。若传 telegram_id，则校验归属。"""
    reject_api_key_direct_cancel(_auth)
    reason = require_cancellation_reason(reason)
    order_number = (order_number or '').strip().lstrip('#')
    order = session.query(Order).filter(Order.order_number == order_number).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if telegram_id:
        user = session.query(User).filter(User.telegram_id == str(telegram_id)).first()
        if not user or order.user_id != user.id:
            raise HTTPException(status_code=403, detail="只能取消自己的订单")

    success, result = db.cancel_order(order_id=order.id, reason=reason or "后台取消", allow_shipped=True)
    if not success:
        raise HTTPException(status_code=400, detail=result)
    return {"success": True, "message": "订单已取消", "result": result}


# ==================== 仓库管理 API ====================

@app.post("/api/warehouses/create")
@_run_blocking_endpoint
def create_warehouse(
    name: str = Form(...),
    address: str = Form(...),
    shipping_fee: float = Form(0.0),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """创建仓库"""
    # 检查仓库是否已存在
    existing = session.query(Warehouse).filter(Warehouse.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="仓库已存在")

    warehouse = Warehouse(name=name, address=address, shipping_fee=shipping_fee)
    session.add(warehouse)
    session.commit()
    session.refresh(warehouse)

    return {
        "id": warehouse.id,
        "name": warehouse.name,
        "address": warehouse.address,
        "shipping_fee": warehouse.shipping_fee,
        "is_active": warehouse.is_active,
        "created_at": warehouse.created_at
    }


@app.get("/api/warehouses")
@_run_blocking_endpoint
def get_warehouses(
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """获取所有仓库"""
    from catalog_retirement import operating_query
    warehouses = operating_query(session, Warehouse, 'warehouse').all()
    return [{
        "id": warehouse.id,
        "name": warehouse.name,
        "address": warehouse.address,
        "shipping_fee": warehouse.shipping_fee,
        "is_active": warehouse.is_active,
        "created_at": warehouse.created_at
    } for warehouse in warehouses]


@app.post("/api/warehouses/{warehouse_id}/update")
@_run_blocking_endpoint
def update_warehouse(
    warehouse_id: int,
    name: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    shipping_fee: Optional[float] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """更新仓库信息"""
    from catalog_retirement import require_operating
    require_operating(session, 'warehouse', warehouse_id)
    warehouse = session.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
    if not warehouse:
        raise HTTPException(status_code=404, detail="仓库不存在")

    if name:
        warehouse.name = name
    if address:
        warehouse.address = address
    if shipping_fee is not None:
        warehouse.shipping_fee = shipping_fee

    session.commit()
    session.refresh(warehouse)

    return {
        "id": warehouse.id,
        "name": warehouse.name,
        "address": warehouse.address,
        "shipping_fee": warehouse.shipping_fee,
        "is_active": warehouse.is_active
    }


@app.post("/api/warehouses/{warehouse_id}/toggle")
@_run_blocking_endpoint
def toggle_warehouse(
    warehouse_id: int,
    is_active: bool = Form(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """启用/禁用仓库"""
    from catalog_retirement import require_operating
    require_operating(session, 'warehouse', warehouse_id)
    warehouse = session.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
    if not warehouse:
        raise HTTPException(status_code=404, detail="仓库不存在")

    warehouse.is_active = is_active
    session.commit()

    return {"success": True, "message": f"仓库已{'启用' if is_active else '禁用'}"}


# ==================== 管理界面 ====================

@app.get("/admin", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_dashboard(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """管理后台"""
    return templates.TemplateResponse(request, "admin/index.html", {})


@app.get("/admin/users", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_users(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """用户管理页面"""
    return templates.TemplateResponse(request, "admin/users.html", {})


@app.get("/admin/products", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_products(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """商品管理页面"""
    return templates.TemplateResponse(request, "admin/products.html", {})


@app.get("/admin/warehouses", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_warehouses(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """仓库管理页面"""
    return templates.TemplateResponse(request, "admin/warehouses.html", {})


@app.get("/admin/orders", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_orders(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """订单管理页面"""
    return templates.TemplateResponse(request, "admin/orders.html", {})


@app.get("/admin/orders/detail", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_order_detail(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """订单详情页面"""
    return templates.TemplateResponse(request, "admin/order_detail.html", {})


@app.get("/test", response_class=HTMLResponse)
@_run_blocking_endpoint
def test_page(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """测试页面"""
    return templates.TemplateResponse(request, "test.html", {})


@app.get("/admin/stats", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_stats_page(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    return templates.TemplateResponse(request, "admin/stats.html", {})


@app.get('/admin/queue-recovery',response_class=HTMLResponse)
@_run_blocking_endpoint
def queue_recovery_page(request:Request):
    redir=require_admin_session(request)
    if redir is not None:return redir
    request.session.setdefault('queue_recovery_csrf',secrets.token_urlsafe(32))
    return templates.TemplateResponse(request,'admin/queue_recovery.html',{'queue_csrf':request.session['queue_recovery_csrf']})


@app.get('/api/admin/queue-recovery')
@_run_blocking_endpoint
def queue_recovery_list(request:Request,category:str='',user:str='',chat:str='',start:str='',end:str='',page:int=1,page_size:int=25,session:Session=Depends(get_db_session)):
    if not is_admin_logged_in(request):raise HTTPException(403,'仅后台管理员可核查任务')
    from pending_work_center import list_work
    try:return list_work(session,category=category,user=user,chat=chat,start=start,end=end,page=page,page_size=page_size)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None


@app.get('/api/admin/queue-recovery/preview')
@_run_blocking_endpoint
def queue_recovery_preview(request:Request,namespace:str,chat_id:str,message_id:int):
    if not is_admin_logged_in(request):raise HTTPException(403,'仅后台管理员可核查任务')
    from queue_recovery_admin import preview
    from config import SESSION_SECRET_KEY
    try:return preview(db,SESSION_SECRET_KEY,namespace,chat_id,message_id)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None


class BillBatchPreviewRequest(BaseModel):
    user_ids:list[StrictInt]
    operation_key:str

class BillBatchConfirmRequest(BaseModel):
    operation_key:str
    expected_count:StrictInt

class BillBatchRetryRequest(BaseModel):
    batch_key:str
    user_id:StrictInt
    reason:str
    operation_key:str

@app.post('/api/payments/bill-batches/retry')
@_run_blocking_endpoint
def bill_batch_retry(request:Request,data:BillBatchRetryRequest):
    operator=bill_batch_operator(request)
    from bill_batches import retry
    try:return retry(db,operator,data.batch_key,data.user_id,data.reason,data.operation_key)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None

def bill_batch_operator(request):
    if not is_admin_logged_in(request):raise HTTPException(403,'仅后台管理员可操作账单批次')
    expected=request.session.get('finance_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Finance-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面重试')
    return request.session.get('admin_username') or 'admin'

@app.post('/api/payments/bill-batches/preview')
@_run_blocking_endpoint
def bill_batch_preview(request:Request,data:BillBatchPreviewRequest):
    operator=bill_batch_operator(request)
    if os.getenv('BILL_BATCH_WRITES_ENABLED','1')!='1':raise HTTPException(503,'批次登记已暂停；原账单和通知记录保留')
    from bill_batches import prepare
    try:return prepare(db,data.user_ids,operator,data.operation_key)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None

@app.post('/api/payments/bill-batches/confirm')
@_run_blocking_endpoint
def bill_batch_confirm(request:Request,data:BillBatchConfirmRequest):
    operator=bill_batch_operator(request)
    if os.getenv('BILL_BATCH_WRITES_ENABLED','1')!='1':raise HTTPException(503,'批次登记已暂停；原账单和通知记录保留')
    from bill_batches import confirm
    try:return confirm(db,operator,data.operation_key,data.expected_count)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None

@app.get('/api/payments/bill-batches/progress')
@_run_blocking_endpoint
def bill_batch_progress(request:Request,operation_key:str):
    if not is_admin_logged_in(request):raise HTTPException(403,'仅后台管理员可查询账单批次')
    from bill_batches import progress
    try:return progress(db,request.session.get('admin_username') or 'admin',operation_key)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None

class QueueRecoveryRequest(BaseModel):
    namespace:str
    chat_id:str
    message_id:StrictInt
    action:str
    version:str
    issued_at:StrictInt
    token:str
    reason:str
    operation_key:str

class PendingDraftCloseRequest(BaseModel):
    token:str
    version:str
    reason:str
    operation_key:str

class PendingContactSupplementRequest(PendingDraftCloseRequest):
    recipient:str=''
    phone:str=''
    address:str=''


@app.post('/api/admin/queue-recovery/apply')
@_run_blocking_endpoint
def queue_recovery_apply(request:Request,data:QueueRecoveryRequest):
    if not is_admin_logged_in(request):raise HTTPException(403,'仅后台管理员可恢复任务')
    expected=request.session.get('queue_recovery_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Queue-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面重试')
    from queue_recovery_admin import apply
    from config import SESSION_SECRET_KEY
    try:return apply(db,SESSION_SECRET_KEY,data.model_dump(),request.session.get('admin_username'),data.reason,data.operation_key)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@app.post('/api/admin/pending-work/close-draft')
@_run_blocking_endpoint
def pending_work_close_draft(request:Request,data:PendingDraftCloseRequest):
    if not is_admin_logged_in(request):raise HTTPException(403,'仅后台管理员可关闭草稿')
    expected=request.session.get('queue_recovery_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Queue-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面重试')
    from pending_work_actions import close_draft
    try:return close_draft(db,token=data.token,expected_version=data.version,
        operator=request.session.get('admin_username'),reason=data.reason,operation_key=data.operation_key)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@app.post('/api/admin/pending-work/supplement-contact')
@_run_blocking_endpoint
def pending_work_supplement_contact(request:Request,data:PendingContactSupplementRequest):
    if not is_admin_logged_in(request):raise HTTPException(403,'仅后台管理员可补充草稿信息')
    expected=request.session.get('queue_recovery_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Queue-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面重试')
    from pending_work_actions import supplement_contact
    try:return supplement_contact(db,token=data.token,expected_version=data.version,
        operator=request.session.get('admin_username'),reason=data.reason,operation_key=data.operation_key,
        recipient=data.recipient,phone=data.phone,address=data.address)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@app.get("/api/admin/stats")
@_run_blocking_endpoint
def get_admin_stats_json(session: Session = Depends(get_db_session), _auth: str = Depends(require_admin_or_api_secret)):
    """兼容旧仪表盘统计 JSON。"""
    total_users = session.query(User).count()
    total_products = session.query(Product).filter(Product.is_active == True).count()
    total_orders = session.query(Order).count()
    pending_orders = session.query(Order).filter(Order.status == "pending").count()
    return {
        "total_users": total_users,
        "total_products": total_products,
        "total_orders": total_orders,
        "pending_orders": pending_orders,
    }


@app.get("/admin/inventory", response_class=HTMLResponse)
@_run_blocking_endpoint
def inventory_upload_page(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    """库存图片上传页面"""
    import secrets
    request.session.setdefault('stock_operations_csrf',secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "admin/inventory.html", {})


@app.post("/api/inventory/extract_text")
async def extract_text_from_image(
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """
    从图片中提取文本（使用OCR）
    支持的格式：PNG, JPG, JPEG
    """
    # 检查文件类型
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="只支持图片文件")

    # 保存临时文件
    temp_dir = tempfile.gettempdir()
    temp_file = os.path.join(temp_dir, f"inventory_{uuid.uuid4().hex}_{file.filename}")

    try:
        with open(temp_file, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        # 使用MCP工具提取文本
        # 这里我们使用web-reader或者直接返回给前端处理
        # 由于需要MCP工具，我们创建一个简化版本

        # 尝试使用PIL和pytesseract（如果已安装）
        try:
            from PIL import Image
            import pytesseract

            # 读取图片并OCR
            image = Image.open(temp_file)
            text = pytesseract.image_to_string(image, lang='chi_sim+eng')

            return {
                "success": True,
                "text": text,
                "message": "文本提取成功"
            }
        except ImportError:
            # 如果没有安装OCR库，返回提示
            return {
                "success": False,
                "error": "OCR库未安装，请先安装: pip install pytesseract pillow",
                "message": "需要安装OCR依赖"
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图片处理失败: {str(e)}")
    finally:
        # 清理临时文件
        if os.path.exists(temp_file):
            os.remove(temp_file)


@app.post("/api/inventory/update_from_text")
async def update_inventory_from_text(
    request: Request,
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """
    从提取的文本更新库存
    """
    try:
        data = await request.json()
        text = data.get("text", "")
        warehouse_name = data.get("warehouse_name", "默认仓库")
        incremental = data.get("incremental", True)

        if not text:
            raise HTTPException(status_code=400, detail="文本内容不能为空")

        # 导入OCR模块
        from inventory_ocr import InventoryUpdater

        updater = InventoryUpdater(db)
        result = updater.update_from_text(text, warehouse_name, incremental)

        return {
            "success": True,
            "result": result.to_dict()
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"库存更新失败: {str(e)}")


@app.get("/api/inventory/warehouses")
@_run_blocking_endpoint
def get_warehouses_list(session: Session = Depends(get_db_session), _auth: str = Depends(require_admin_or_api_secret)):
    """获取所有仓库列表（用于库存更新页面）"""
    warehouses = session.query(Warehouse).filter(Warehouse.is_active == True).all()
    return [{"id": w.id, "name": w.name} for w in warehouses]


@app.post("/api/inventory/process_image")
async def process_inventory_image(
    file: UploadFile = File(...),
    warehouse_name: str = Form(default="默认仓库"),
    incremental: bool = Form(default=True),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    """
    一键处理：上传图片 -> OCR识别 -> 更新库存
    """
    # 检查文件类型
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="只支持图片文件")

    # 保存临时文件
    temp_dir = tempfile.gettempdir()
    temp_file = os.path.join(temp_dir, f"inventory_{uuid.uuid4().hex}_{file.filename}")

    try:
        with open(temp_file, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        # OCR提取文本
        try:
            from PIL import Image
            import pytesseract

            image = Image.open(temp_file)
            text = pytesseract.image_to_string(image, lang='chi_sim+eng')

            # 更新库存
            from inventory_ocr import InventoryUpdater

            updater = InventoryUpdater(db)
            result = updater.update_from_text(text, warehouse_name, incremental)

            return {
                "success": True,
                "extracted_text": text,
                "result": result.to_dict()
            }

        except ImportError:
            return {
                "success": False,
                "error": "OCR库未安装",
                "message": "请运行: pip install pytesseract pillow"
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)




@app.get('/api/orders/{order_id:int}/receipt-evidence')
@_run_blocking_endpoint
def get_receipt_evidence(order_id:int,session:Session=Depends(get_db_session),_auth:str=Depends(require_admin_or_api_secret)):
    """Evidence only. This endpoint never queues or repairs a delivery."""
    order=session.get(Order,order_id)
    if order is None:raise HTTPException(404,'订单不存在')
    from sqlalchemy import text,inspect
    from success_receipt_audit import records_for_order
    try:attempts=records_for_order(order.order_number)
    except (OSError,ValueError,KeyError,TypeError):raise HTTPException(503,'回执证据暂不可读取；未触发重发') from None
    outbox=[];tasks=[]
    tables=set(inspect(session.connection()).get_table_names())
    params=dict(chat=order.source_chat_id,message=order.source_message_id)
    if 'telegram_execution_receipts' in tables:
        outbox=[dict(r) for r in session.execute(text('SELECT event_key,status,attempts,delivered_message_id,error_code FROM telegram_execution_receipts WHERE chat_id=:chat AND source_message_id=:message'),params).mappings()]
    if 'telegram_order_jobs' in tables:
        import hashlib
        clause='(j.chat_id=:chat AND j.message_id=:message)'
        if 'telegram_execution_commits' in tables:
            clause+=' OR EXISTS (SELECT 1 FROM telegram_execution_commits c WHERE c.order_id=:oid AND c.namespace=j.namespace AND c.chat_id=j.chat_id AND c.message_id=j.message_id)'
        tasks=[dict(task_id=hashlib.sha256(('%s:%s:%s'%(r['namespace'],r['chat_id'],r['message_id'])).encode()).hexdigest()[:16],state=r['state']) for r in session.execute(text('SELECT j.namespace,j.chat_id,j.message_id,j.state FROM telegram_order_jobs j WHERE '+clause),dict(params,oid=order.id)).mappings()]
    from success_receipt_audit import delivery_summary
    status=delivery_summary(attempts,outbox)
    return dict(order_id=order.id,order_number=order.order_number,status=status,tasks=tasks,
        direct_attempts=attempts,outbox=outbox,historical_state_modified=False,resend_triggered=False)

# ==================== 用户专属单价 / 人工收账 / 统计 / 打印 ====================

@app.get("/api/users/{user_id}/prices")
@_run_blocking_endpoint
def get_user_prices(user_id: int, session: Session = Depends(get_db_session), _auth: str = Depends(require_admin_or_api_secret)):
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    parent = aliased(Product)
    rows = (
        session.query(Product, parent, UserProductPrice)
        .outerjoin(parent, Product.parent_id == parent.id)
        .outerjoin(
            UserProductPrice,
            and_(
                UserProductPrice.user_id == user_id,
                UserProductPrice.product_id == Product.id,
            ),
        )
        .filter(
            Product.is_active == True,
            Product.is_parent == False,
            Product.is_archived == False,
            or_(Product.parent_id.is_(None), and_(parent.is_archived == False, parent.is_active == True)),
        )
        .order_by(Product.parent_id.asc(), Product.id.asc())
        .all()
    )
    retired = set(__import__('catalog_retirement').retired_ids(session,'product'))
    rows = [row for row in rows if row[0].id not in retired and row[0].parent_id not in retired]
    logical = __import__('logical_categories').groups(session, [row[0] for row in rows])
    logical_by_parent = {id: group for group in logical for id in group['category_ids']}
    warehouse_names = {w.id:w.name for w in session.query(Warehouse).all()}
    category_prices={row.parent_id:row.unit_price for row in session.query(UserCategoryPrice).filter_by(user_id=user_id)}
    result = []
    for product, parent_product, custom_price_row in rows:
        custom_price = custom_price_row.unit_price if custom_price_row else None
        default_price = product.get_price_for_agent_level(user.agent_level)
        category_price=category_prices.get(product.parent_id)
        applied=custom_price if custom_price is not None else category_price
        unit = __import__('dynamic_prices').custom(product,applied) if applied is not None else None
        if unit is None:
            unit = default_price
        result.append({
            'product_id': product.id,
            'product_name': product.name,
            'unit_price': unit,
            'default_price': default_price,
            'custom_price': custom_price,
            'is_custom': applied is not None,
            'category_price': category_price,
            'price_source': 'flavor' if custom_price is not None else 'category' if category_price is not None else 'level',
            'parent_id': product.parent_id,
            'logical_category_id': logical_by_parent.get(product.parent_id, {}).get('id'),
            'warehouse_id': product.default_warehouse_id,
            'warehouse_name': warehouse_names.get(product.default_warehouse_id, ''),
            'parent_name': parent_product.name if parent_product else '默认分类',
            'category_display_id': _display_serial('C', parent_product.category_serial) if parent_product and parent_product.category_serial else '',
            'product_display_id': _display_serial('P', product.product_serial) if product.product_serial else '',
            'regular_price': product.regular_price,
            'cost_price': product.cost_price,
        })
    return {
        'user_id': user_id,
        'debt': db.calc_user_debt(user_id, session=session),
        'balance': user.balance,
        'prices': result,
        'logical_categories': [group for group in logical if group['product_count']],
    }


@app.post("/api/users/{user_id}/prices")
@_run_blocking_endpoint
def set_user_price(
    user_id: int,
    product_id: Optional[int] = Form(None),
    parent_id: Optional[int] = Form(None),
    logical_category_id: Optional[int] = Form(None),
    expected_product_ids: str = Form(''),
    unit_price: str = Form(...),
    price_review_token: str = Form(''),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    if not session.connection().connection.driver_connection.in_transaction:
        session.execute(sql_text('BEGIN IMMEDIATE'))
    if logical_category_id is not None or parent_id is not None:
        if not session.connection().connection.driver_connection.in_transaction:
            session.execute(sql_text('BEGIN IMMEDIATE'))
    from catalog_retirement import require_operating
    require_operating(session, 'product', product_id)
    require_operating(session, 'product', parent_id)
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    unit_price = _sale_form_amount(unit_price)
    if not __import__('math').isfinite(unit_price) or unit_price < 0:
        raise HTTPException(status_code=400, detail="单价不能小于 0")

    if logical_category_id is not None and (parent_id is not None or product_id is not None):
        raise HTTPException(400, '逻辑分类与实体商品目标不能混用')
    if parent_id is not None or logical_category_id is not None:
        parent_ids = (__import__('logical_categories').category_ids(session, logical_category_id)
                      if logical_category_id is not None else [parent_id])
        selected_id = logical_category_id if logical_category_id is not None else parent_id
        parent = session.query(Product).filter(Product.id == selected_id, Product.is_parent == True).first()
        if not parent:
            raise HTTPException(status_code=404, detail="商品品类不存在")
        products = __import__('catalog_retirement').operating_query(session,Product,'product').filter(
            Product.parent_id.in_(parent_ids),
            Product.is_parent == False,
            Product.is_active == True,
            Product.is_archived == False,
        ).order_by(Product.id.asc()).all()
        if not products:
            raise HTTPException(status_code=400, detail="该品类下没有可设置的口味")
        if logical_category_id is not None:
            try:
                expected = json.loads(expected_product_ids)
                if not isinstance(expected,list) or not expected or any(type(id) is not int for id in expected):
                    raise ValueError('Invalid IDs')
            except (ValueError,TypeError):
                raise HTTPException(400,'请先刷新分类并确认具体商品范围') from None
            if set(expected) != {p.id for p in products}:
                raise HTTPException(409,'商品目录已变化，未修改任何价格，请刷新后重新确认')
        retained_overrides = [r.product_id for r in session.query(UserProductPrice).filter(
            UserProductPrice.user_id == user_id,
            UserProductPrice.product_id.in_([p.id for p in products]))]
        from sale_price_review import require_confirmation
        from dynamic_prices import custom
        require_confirmation(session,[(p,custom(p,unit_price)) for p in products if p.id not in retained_overrides],
            dict(user_id=user_id,category_ids=sorted(parent_ids),requested_price=unit_price,
                 price_state=__import__('price_override_admin').preview(session,user_id)['token']),price_review_token)
        for target_parent in parent_ids:
            row=session.query(UserCategoryPrice).filter_by(user_id=user_id,parent_id=target_parent).first()
            if row:row.unit_price=unit_price
            else:session.add(UserCategoryPrice(user_id=user_id,parent_id=target_parent,unit_price=unit_price))
        session.commit()
        return {
            'success': True,
            'user_id': user_id,
            'parent_id': parent_id,
            'logical_category_id': logical_category_id,
            'category_ids': parent_ids,
            'product_ids': [p.id for p in products],
            'parent_name': parent.name,
            'unit_price': unit_price,
            'updated_count': len(products),
            'inherited_count': len(products) - len(retained_overrides),
            'retained_flavor_override_ids': retained_overrides,
            'message': (f"分类价已保存，{len(products) - len(retained_overrides)} 个口味跟随；"
                        f"保留 {len(retained_overrides)} 条口味覆盖，历史来源不明的覆盖需另行确认"),
        }

    if product_id is None:
        raise HTTPException(status_code=400, detail="请提供商品或商品品类")
    product = session.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    if product.is_parent:
        raise HTTPException(status_code=400, detail="分类不能设置用户专属单价，请选择具体口味")
    from sale_price_review import require_confirmation
    require_confirmation(session,[(product,__import__('dynamic_prices').custom(product,unit_price))],
        dict(user_id=user_id,price_state=__import__('price_override_admin').preview(session,user_id)['token']),price_review_token)
    row = session.query(UserProductPrice).filter(
        UserProductPrice.user_id == user_id,
        UserProductPrice.product_id == product_id,
    ).first()
    if row:
        row.unit_price = unit_price
    else:
        row = UserProductPrice(
            user_id=user_id,
            product_id=product_id,
            unit_price=unit_price,
        )
        session.add(row)
    session.commit()
    session.refresh(row)
    return {
        'success': True,
        'user_id': user_id,
        'product_id': product_id,
        'product_name': product.name,
        'unit_price': row.unit_price,
    }


@app.post("/api/users/{user_id}/prices/batch")
@_run_blocking_endpoint
def set_user_prices_batch(
    user_id: int,
    payload: UserPriceBatchRequest,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
    request: Request = None,
):
    """一次保存多个用户专属价，避免前端并发逐条写入 SQLite。"""
    if not session.connection().connection.driver_connection.in_transaction:
        session.execute(sql_text('BEGIN IMMEDIATE'))
    if payload.canonical_all_warehouses and not session.connection().connection.driver_connection.in_transaction:
        session.execute(sql_text('BEGIN IMMEDIATE'))
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not payload.prices:
        raise HTTPException(status_code=400, detail="没有可保存的价格")
    if len(payload.prices) > 2000:
        raise HTTPException(status_code=400, detail="单次最多保存 2000 个商品价格")

    updates = {}
    for item in payload.prices:
        product_id = int(item.product_id)
        unit_price = round(float(item.unit_price), 2)
        if product_id <= 0 or not __import__('math').isfinite(unit_price) or unit_price < 0:
            raise HTTPException(status_code=400, detail="商品或单价格式无效")
        updates[product_id] = unit_price

    canonical_price = None
    if payload.canonical_all_warehouses:
        if len(updates) != 1:
            raise HTTPException(400, '跨仓统一必须逐个Canonical口味指定价格')
        source_id, canonical_price = next(iter(updates.items()))
        from catalog_retirement import require_operating, operating_query
        require_operating(session, 'product', source_id)
        source = session.get(Product, source_id)
        if not source or source.is_parent or not source.parent_id or not source.is_active:
            raise HTTPException(400, '请选择有效正式口味')
        parents = __import__('logical_categories').category_ids(session, source.parent_id)
        peers = operating_query(session, Product, 'product').filter(Product.parent_id.in_(parents),
            Product.name == source.name, Product.is_parent == False,
            Product.is_active == True, Product.is_archived == False).all()
        actual = {p.id for p in peers}
        expected = payload.expected_product_ids
        if not expected or len(set(expected)) != len(expected) or set(expected) != actual:
            raise HTTPException(409, '跨仓商品范围已变化，请刷新后重新确认，未统一价格')
        if len({p.default_warehouse_id for p in peers}) != len(peers):
            raise HTTPException(409, '同仓Canonical口味不唯一，不能自动统一')
        # The explicitly supplied canonical price is the final selling price;
        # preserve existing offset mechanics without adding the surcharge twice.
        from dynamic_prices import offset
        updates = {p.id:round(canonical_price-offset(p),2) for p in peers}
        if any(value < 0 for value in updates.values()):
            raise HTTPException(400, '指定价格低于现有动态加价，需先人工核实价格规则')

    product_ids = list(updates)
    products = session.query(Product.id).filter(
        Product.id.in_(product_ids),
        Product.is_parent == False,
    ).all()
    existing_product_ids = {product_id for (product_id,) in products}
    missing_product_ids = sorted(set(product_ids) - existing_product_ids)
    if missing_product_ids:
        raise HTTPException(
            status_code=404,
            detail=f"商品不存在或不是可售口味: {', '.join(map(str, missing_product_ids[:10]))}",
        )

    existing = {
        row.product_id: row
        for row in session.query(UserProductPrice).filter(
            UserProductPrice.user_id == user_id,
            UserProductPrice.product_id.in_(product_ids),
        ).all()
    }
    from sale_price_review import require_confirmation
    require_confirmation(session,[(session.get(Product,pid),__import__('dynamic_prices').custom(session.get(Product,pid),price)) for pid,price in updates.items()],
        dict(user_id=user_id,canonical=payload.canonical_all_warehouses,
             price_state=__import__('price_override_admin').preview(session,user_id)['token']),payload.price_review_token)
    if canonical_price is not None:
        from models import PriceOverrideAudit
        actor = (request.session.get('admin_username') if request is not None else None) or 'authenticated-price-api'
        session.add(PriceOverrideAudit(user_id=user_id, actor=actor,
            before_json=json.dumps({'action':'canonical_price_explicit_unification',
                'requested_final_price':canonical_price,'product_ids':sorted(updates),
                'before':{str(pid):existing[pid].unit_price if pid in existing else None for pid in updates}},ensure_ascii=False)))
    for product_id, unit_price in updates.items():
        row = existing.get(product_id)
        if row:
            row.unit_price = unit_price
        else:
            session.add(UserProductPrice(
                user_id=user_id,
                product_id=product_id,
                unit_price=unit_price,
            ))
    session.commit()
    return {
        'success': True,
        'user_id': user_id,
        'updated_count': len(updates),
    }


@app.post("/api/users/{user_id}/update_profile")
@_run_blocking_endpoint
def update_user_profile(
    user_id: int,
    display_name: Optional[str] = Form(None),
    preferred_pay_method: Optional[str] = Form(None),
    remark: Optional[str] = Form(None),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if display_name is not None:
        user.display_name = display_name
    if preferred_pay_method is not None:
        user.preferred_pay_method = preferred_pay_method
    if remark is not None:
        user.remark = remark
    session.commit()
    return {'success': True, 'message': '用户资料已更新'}


@app.get("/api/payments/usdt-rate")
async def get_usdt_payment_rate(
    refresh: bool = False,
    _auth: str = Depends(require_admin_or_api_secret),
):
    try:
        info = await resolve_usdt_cny_rate(force=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        'success': True,
        'base': 'USDT',
        'quote': 'CNY',
        'rate': info['rate'],
        'source': info.get('source', 'configured_provider'),
        'cached': bool(info.get('cached')),
        'fetched_at': datetime.utcfromtimestamp(info['fetched_at']).isoformat() + 'Z',
    }


@app.post("/api/payments/create")
async def create_payment(
    user_id: int = Form(...),
    amount: float = Form(...),
    method: str = Form('cny'),
    order_id: Optional[int] = Form(None),
    note: Optional[str] = Form(None),
    operator: Optional[str] = Form('admin'),
    operation_key: str = Form(...),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    method = str(method or 'cny').strip().lower()
    original_amount = float(amount)
    if original_amount <= 0:
        raise HTTPException(status_code=400, detail='收款金额必须大于0')
    settlement_amount = original_amount
    exchange_rate = None
    exchange_currency = None
    if method == 'usdt':
        try:
            # A payment must never rely on an older in-process cache entry.
            # The applied rate is persisted with the payment as an audit snapshot.
            rate_info = await resolve_usdt_cny_rate(force=True)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        exchange_rate = float(rate_info['rate'])
        exchange_currency = 'USDT'
        settlement_amount = float(
            (Decimal(str(original_amount)) * Decimal(str(exchange_rate))).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP,
            )
        )
    elif method == 'cny':
        exchange_rate = 1.0
        exchange_currency = 'CNY'

    success, result = await asyncio.to_thread(db.record_payment,
        user_id=user_id,
        amount=settlement_amount,
        method=method,
        order_id=order_id,
        note=note,
        operator=operator,
        original_amount=original_amount,
        exchange_rate=exchange_rate,
        exchange_currency=exchange_currency,
        operation_key=operation_key if isinstance(operation_key,str) else None,
    )
    if not success:
        raise HTTPException(status_code=400, detail=result)

    # record_payment() commits independently. Telegram notices are best effort
    # and must never turn a completed payment into an API failure.
    payment_notice = {
        'attempted': False,
        'sent': False,
        'detail': '未找到来源群订单',
        'sent_count': 0,
        'failed_count': 0,
    }
    try:
        session.expire_all()
        # Notification routing is now the account's active dedicated group,
        # with the established private-chat fallback. Never fan out to historical
        # source groups that may have been replaced or unbound.
        from account_notifications import dispatch,payment_text
        payment_notice=await dispatch(db,'payment:'+str(result['payment_id']),user_id,
            'payment',payment_text(result),send_telegram_message_async)
    except Exception as exc:
        logger.exception('收款后账单群通知处理失败: %s', exc)
        payment_notice.update({
            'attempted': True,
            'sent': False,
            'detail': str(exc),
        })

    result = dict(result or {})
    result['payment_notice'] = payment_notice
    return {
        'success': True,
        'result': result,
        'payment_notice': payment_notice,
    }


@app.post('/api/receivables/create')
async def create_manual_receivable(request:Request,user_id:int=Form(...),amount:str=Form(...),
    reason:str=Form(...),operation_key:str=Form(...),_auth:str=Depends(require_admin_or_api_secret)):
    if _auth!='session':raise HTTPException(403,'仅后台管理员可新增待付金额')
    if os.getenv('MANUAL_RECEIVABLES_WRITES_ENABLED','1')!='1':
        raise HTTPException(503,'新增待付已暂停；既有账款仍保留并可正常结算')
    expected=request.session.get('finance_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Finance-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面重试')
    from manual_receivables import add
    try:
        result=await asyncio.to_thread(add,db,user_id,amount,reason,request.session.get('admin_username'),operation_key)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from None
    return dict(success=True,result=result)


@app.get('/api/receivables')
@_run_blocking_endpoint
def list_manual_receivables(user_id:Optional[int]=None,limit:int=Query(100,ge=1,le=500),
    session:Session=Depends(get_db_session),_auth:str=Depends(require_admin_or_api_secret)):
    sql='''SELECT r.*,n.status AS notification_status,n.last_error AS notification_error
        FROM manual_receivables r LEFT JOIN telegram_notification_events n
        ON n.event_key='receivable:'||r.id'''
    if user_id is not None:sql+=' WHERE r.user_id=:user'
    sql+=' ORDER BY r.id DESC LIMIT :limit'
    return [dict(row,amount=row['amount_cents']/100,paid=row['paid_cents']/100)
        for row in session.execute(sql_text(sql),dict(user=user_id,limit=limit)).mappings()]


@app.post('/api/receivables/{identifier}/retry-notice')
async def retry_receivable_notice(identifier:int,request:Request,reason:str=Form(...),operation_key:str=Form(...),
    _auth:str=Depends(require_admin_or_api_secret)):
    if _auth!='session':raise HTTPException(403,'仅后台管理员可恢复通知')
    expected=request.session.get('finance_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Finance-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面重试')
    from manual_receivables import retry_notice
    try:
        result=await asyncio.to_thread(retry_notice,db,identifier,request.session.get('admin_username'),reason,operation_key)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None
    return dict(success=True,result=result)


@app.post('/api/payments/push-bill')
async def push_account_bill(request:Request,user_id:int=Form(...),operation_key:str=Form(...),
    _auth:str=Depends(require_admin_or_api_secret),session:Session=Depends(get_db_session)):
    if _auth=='session':
        expected=request.session.get('finance_csrf','')
        if not expected or not secrets.compare_digest(expected,request.headers.get('X-Finance-CSRF','')):
            raise HTTPException(403,'请求校验失败，请刷新页面重试')
    if not 16<=len(operation_key)<=100:
        raise HTTPException(400,'操作标识无效，请刷新页面重试')
    from telegram_accounts import summary,notification_target
    from account_notifications import dispatch,bill_text
    from cumulative_bill_snapshot import capture
    try:
        facts=capture(session,user_id)
        target=notification_target(session,user_id)
        if not target or not str(target).startswith('-'):
            raise ValueError('该用户没有可用的历史接单群，未推送账单')
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from None
    # Release the read snapshot before the outbox uses a separate writer.
    session.commit()
    result=await dispatch(db,f'manual-bill:{user_id}:{operation_key}',user_id,'manual_bill',
        facts['text'],send_telegram_message_async)
    with db.SessionLocal() as notice_session:
        notice_status=notice_session.execute(sql_text('SELECT status FROM telegram_notification_events WHERE event_key=:key'),
            {'key':f'manual-bill:{user_id}:{operation_key}'}).scalar()
    return dict(success=notice_status=='sent',notice=result,notice_status=notice_status)


@app.post("/api/payments/apply_prepaid")
async def apply_prepaid_payment(
    user_id: int = Form(...),
    amount: Optional[float] = Form(None),
    order_id: Optional[int] = Form(None),
    note: Optional[str] = Form(None),
    operator: Optional[str] = Form('admin'),
    operation_key: str = Form(...),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    success, result = await asyncio.to_thread(db.apply_prepaid_balance,
        user_id=user_id,
        amount=amount,
        order_id=order_id,
        note=note,
        operator=operator,
        operation_key=operation_key if isinstance(operation_key,str) else None,
    )
    if not success:
        raise HTTPException(status_code=400, detail=result)
    try:
        from account_notifications import dispatch
        message = f"💰 预存抵扣成功\n本次抵扣：¥{result['amount']:.2f}\n当前待结账：¥{result['user_debt']:.2f}\n当前预存：¥{result['user_balance']:.2f}"
        result['notice'] = await dispatch(db, 'payment:'+str(result['payment_id']), user_id,
                                          'prepaid', message, send_telegram_message_async)
    except Exception:
        logger.exception('预存抵扣已提交，通知处理失败')
    return {'success': True, 'result': result}


@app.post("/api/users/{user_id}/balance_adjust")
async def adjust_user_balance_api(
    user_id: int,
    mode: str = Form(...),
    amount: float = Form(...),
    note: str = Form(...),
    operator: Optional[str] = Form('admin'),
    operation_key: str = Form(...),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    success, result = db.adjust_user_balance(
        user_id=user_id,
        mode=mode,
        amount=amount,
        note=note,
        operator=operator,
        operation_key=operation_key if isinstance(operation_key,str) else None,
    )
    if not success:
        raise HTTPException(status_code=400, detail=result)
    try:
        from account_notifications import dispatch
        message=f"💰 预存余额调整\n本次变动：{float(result['delta']):+.2f}元\n当前预存：¥{float(result['new_balance']):.2f}"
        result['notice']=await dispatch(db,'balance:'+str(result['transaction_id']),user_id,'balance',message,send_telegram_message_async)
    except Exception:
        logger.exception('预存调整已提交，通知处理失败')
    return {'success': True, 'result': result}


@app.get("/api/payments")
@_run_blocking_endpoint
def list_payments(
    user_id: Optional[int] = None,
    limit: int = 100,
    search: str = '',
    offset: int = 0,
    paginated: bool = False,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    from payment_history import history
    try:
        result=history(session,user_id=user_id,search=search,limit=limit,offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc))
    return result if paginated else result['items']


@app.get("/api/stats/daily")
@_run_blocking_endpoint
def stats_daily(day: Optional[str] = None, _auth: str = Depends(require_admin_or_api_secret)):
    return db.daily_stats(day)


def _parse_export_date(value: Optional[str], field_name: str):
    """Parse YYYY-MM-DD date from export query parameters."""
    from datetime import datetime as dt

    if not value:
        return None
    try:
        return dt.strptime(value[:10], "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field_name}日期格式错误，应为 YYYY-MM-DD")


def _order_customer_name(order: Order) -> str:
    user = order.user
    if not user:
        return ""
    return (
        getattr(user, "display_name", None)
        or getattr(user, "telegram_name", None)
        or getattr(user, "username", None)
        or getattr(user, "telegram_id", None)
        or ""
    )


def _order_status_label(status: str) -> str:
    return {
        "pending": "待录单",
        "split_pending": "分单订单",
        "confirmed": "已录单",
        "recorded": "已录单",
        "shipped": "已发货",
        "cancelled": "已取消",
    }.get(status or "", status or "")


def _public_order_number(order: Order) -> str:
    """Return the weekly business number without changing legacy references."""
    return str(getattr(order, "business_order_number", None) or order.order_number)


def _fhd_status_label(status: str) -> str:
    return {
        "not_pushed": "未推送",
        "success": "已推送",
        "failed": "推送失败",
        "partial": "部分推送",
    }.get(status or "", status or "")


def _split_export_conflicts(orders: List[Order]) -> List[dict]:
    """找出同一地址同一天被选中导出的多笔正式分单。"""
    from datetime import timedelta

    buckets = {}
    for order in orders or []:
        if not (
            getattr(order, 'split_group_id', None)
            or int(getattr(order, 'split_count', 1) or 1) > 1
        ):
            continue
        address_key = package_rules.address_key(getattr(order, 'recipient_address', None))
        if not address_key:
            continue
        created_at = getattr(order, 'created_at', None) or datetime.utcnow()
        local_day = business_format(created_at)[:10]
        buckets.setdefault((local_day, address_key), []).append(order)

    conflicts = []
    for (day, _address_key), rows in buckets.items():
        if len(rows) <= 1:
            continue
        rows = sorted(rows, key=lambda value: (value.split_index or 0, value.id or 0))
        first = rows[0]
        conflicts.append({
            'day': day,
            'recipient_name': first.recipient_name,
            'recipient_phone': first.recipient_phone,
            'recipient_address': first.recipient_address,
            'split_group_id': first.split_group_id,
            'order_ids': [row.id for row in rows],
            'order_numbers': [row.order_number for row in rows],
            'count': len(rows),
        })
    return conflicts


def _order_is_recorded(order: Order) -> bool:
    """Whether an order has a durable export or legacy recording fact."""
    if getattr(order, 'split_group_id', None):
        from split_management import _recording_succeeded
        return _recording_succeeded(order)
    return bool(
        getattr(order, "recorded_at", None)
        or
        getattr(order, "status", None) in ('confirmed', 'recorded', 'shipped')
        or
        int(getattr(order, "fhd_push_count", 0) or 0) > 0
        or str(getattr(order, "fhd_push_status", "") or "") in {"success", "partial"}
    )


def _parse_order_date(value: Optional[str], field_name: str, end: bool = False):
    """Parse an inclusive YYYY-MM-DD filter into a naive datetime."""
    if not value:
        return None
    from business_time_range import boundary
    try:return boundary(value,end=end)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None


def _latest_cancel_request(order: Order):
    requests = list(getattr(order, "cancellation_requests", None) or [])
    if not requests:
        return None
    return max(requests, key=lambda item: (item.created_at or datetime.min, item.id or 0))


def _query_shipped_orders_for_export(session: Session, start, end_exclusive):
    return (
        session.query(Order)
        .options(
            selectinload(Order.user),
            selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent),
            selectinload(Order.items).selectinload(OrderItem.warehouse),
        )
        .filter(Order.shipped_at >= start, Order.shipped_at < end_exclusive)
        .order_by(Order.shipped_at.asc(), Order.id.asc())
        .all()
    )


def _build_shipping_orders_csv_response(
    orders: List[Order], start, end, filename: str, session: Optional[Session] = None,
) -> Response:
    import csv
    import io

    label = f"{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}（北京时间）"
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["发货单表格", label])
    writer.writerow(["导出订单数", len(orders), "导出时间", business_now().strftime("%Y-%m-%d %H:%M:%S")])
    writer.writerow([])
    writer.writerow([
        "订单号",
        "客户",
        "收件人",
        "电话",
        "详细地址",
        "商品明细",
        "商品数量",
        "商品金额",
        "运费",
        "应付总额",
        "订单状态",
        "物流状态",
        "物流公司",
        "运单号",
        "发货时间",
    ])

    total_qty = 0
    total_amount = 0.0
    total_shipping_fee = 0.0
    total_grand = 0.0
    for order in orders:
        grouped_items = _group_order_items(order.items)
        item_details = [group['text'] for group in grouped_items]
        order_qty = sum(int(group['quantity'] or 0) for group in grouped_items)

        order_amount = float(order.total_amount or 0)
        order_shipping_fee = float(order.total_shipping_fee or 0)
        order_grand = float(order.grand_total or 0)
        total_qty += order_qty
        total_amount += order_amount
        total_shipping_fee += order_shipping_fee
        total_grand += order_grand
        writer.writerow([
            _public_order_number(order),
            _order_customer_name(order),
            order.recipient_name,
            order.recipient_phone,
            order.recipient_address,
            "；".join(item_details),
            order_qty,
            round(order_amount, 2),
            round(order_shipping_fee, 2),
            round(order_grand, 2),
            _order_status_label(order.status),
            _fhd_status_label(getattr(order, "fhd_push_status", "") or ""),
            getattr(order, "fhd_cp_name", "") or getattr(order, "fhd_cp_code", "") or "",
            ", ".join(_fhd_waybill_codes(order, session)),
            business_format(order.shipped_at),
        ])

    writer.writerow([])
    writer.writerow([
        "合计",
        "",
        "",
        "",
        "",
        f"{len(orders)}单",
        total_qty,
        round(total_amount, 2),
        round(total_shipping_fee, 2),
        round(total_grand, 2),
        "",
        "",
        "",
        "",
        "",
    ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/stats/daily/export")
@_run_blocking_endpoint
def export_daily_shipping_stats(
    day: Optional[str] = None,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    import csv
    import io
    from datetime import datetime as dt, timedelta

    if day:
        start = dt.strptime(day[:10], '%Y-%m-%d')
    else:
        now = business_now()
        start = dt(now.year, now.month, now.day)
    end = start + timedelta(days=1)
    stats = db.daily_stats(start.strftime('%Y-%m-%d'))
    from business_time_range import bounds
    start,end=bounds(start.strftime('%Y-%m-%d'),start.strftime('%Y-%m-%d'),required=True)
    shipped = _query_shipped_orders_for_export(session, start, end)

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['每日发货统计', stats['day']])
    writer.writerow([])
    writer.writerow(['订单数', '发货数', '销售额', '发货金额', '补货数量', '出库数量', '收款', '总欠款'])
    writer.writerow([
        stats.get('orders_created', 0),
        stats.get('orders_shipped', 0),
        stats.get('orders_amount', 0),
        stats.get('shipped_amount', 0),
        stats.get('inbound_qty', 0),
        stats.get('outbound_qty', 0),
        stats.get('payments_amount', 0),
        stats.get('total_debt', 0),
    ])
    writer.writerow([])
    writer.writerow(['已发货订单'])
    writer.writerow(['订单号', '用户', '收件人', '电话', '地址', '商品明细', '金额', '风火递状态', '物流公司', '运单号', '发货时间'])
    for order in shipped:
        user_name = _order_customer_name(order)
        item_details = [group['text'] for group in _group_order_items(order.items)]
        writer.writerow([
            _public_order_number(order),
            user_name,
            order.recipient_name,
            order.recipient_phone,
            order.recipient_address,
            '；'.join(item_details),
            round(float(order.grand_total or 0), 2),
            getattr(order, 'fhd_push_status', '') or '',
            getattr(order, 'fhd_cp_name', '') or getattr(order, 'fhd_cp_code', '') or '',
            ", ".join(_fhd_waybill_codes(order, session)),
            business_format(order.shipped_at),
        ])
    writer.writerow([])
    writer.writerow(['补货汇总'])
    writer.writerow(['商品品类', '口味 / 商品', '仓库', '数量'])
    for item in stats.get('inbound_items', []):
        writer.writerow([item.get('parent_name') or '默认分类', item.get('product_name'), item.get('warehouse_name'), item.get('quantity')])
    writer.writerow([])
    writer.writerow(['出库汇总'])
    writer.writerow(['商品品类', '口味 / 商品', '仓库', '数量'])
    for item in stats.get('outbound_items', []):
        writer.writerow([item.get('parent_name') or '默认分类', item.get('product_name'), item.get('warehouse_name'), abs(int(item.get('quantity') or 0))])

    filename = f"daily_shipping_{stats['day']}.csv"
    return Response(
        content=output.getvalue(),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.get("/api/stats/shipping/export")
@_run_blocking_endpoint
def export_shipping_orders_by_date(
    start: Optional[str] = None,
    end: Optional[str] = None,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """按日期区间导出后台发货单表格。start/end=YYYY-MM-DD，包含结束日。"""
    from datetime import datetime as dt, timedelta

    from business_time_range import bounds,OFFSET
    if not start and not end:start=end=(dt.utcnow()+OFFSET).strftime('%Y-%m-%d')
    elif not start:start=end
    elif not end:end=start
    try:start_dt,end_exclusive=bounds(start,end,required=True)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None
    orders = _query_shipped_orders_for_export(session, start_dt, end_exclusive)
    filename='shipping_orders_'+re.sub(r'[^0-9A-Za-z_-]','-',start+'_'+end)+'.csv'
    return _build_shipping_orders_csv_response(orders,start_dt+OFFSET,end_exclusive+OFFSET-timedelta(microseconds=1),filename,session=session)


@app.get("/api/orders/{order_id}/print")
@_run_blocking_endpoint
def print_order(
    order_id: int,
    template: str = Query('shipping'),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """Print templates: shipping | statement | simple"""
    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    user = order.user
    customer = (getattr(user, 'display_name', None) if user else None) or (user.username if user else None) or (user.telegram_id if user else '') or ''
    paid = float(getattr(order, 'paid_amount', 0) or 0)
    total = float(order.grand_total or 0)
    due = max(0.0, total - paid)
    pay_status = getattr(order, 'payment_status', 'unpaid') or 'unpaid'
    debt = db.calc_user_debt(user.id, session=session) if user else 0.0

    item_rows = []
    for item in order.items:
        item_rows.append({
            'product_name': item.product.name if item.product else '',
            'parent_name': item.product.parent.name if item.product and item.product.parent else '',
            'display_name': _display_order_item_name(item),
            'warehouse_name': item.warehouse.name if item.warehouse else '',
            'quantity': item.quantity,
            'unit': __import__('product_units').historical(session,item),
            'unit_price': float(item.unit_price or 0),
            'amount': float((item.unit_price or 0) * (item.quantity or 0)),
        })
    grouped_rows = _group_order_items(item_rows)

    template = (template or 'shipping').lower().strip()
    if template not in ('shipping', 'statement', 'simple'):
        template = 'shipping'

    def money(v):
        return 'Y' + format(float(v or 0), '.2f')

    # Use Y as ASCII stand-in in pure text; HTML uses full-width yuan via entity
    def money_html(v):
        return '&yen;' + format(float(v or 0), '.2f')

    public_order_number = _public_order_number(order)
    if template == 'shipping':
        title = '发货单'
        lines = [
            '======== SHIPPING SLIP ========',
            f'Order: {public_order_number}',
            f'Time: {order.created_at}',
            f'Status: {order.status}',
            f'Customer: {customer}',
            '------------------------------',
            f'Recipient: {order.recipient_name}',
            f'Phone: {order.recipient_phone}',
            f'Address: {order.recipient_address}',
            '------------------------------',
            'Items:',
        ]
        for group in grouped_rows:
            warehouse_text = f" [{'/'.join(group['warehouses'])}]" if group['warehouses'] else ''
            lines.append(f"- {group['text']}{warehouse_text}")
        lines += [
            '------------------------------',
            f'Products: {money(order.total_amount)}',
            f'Shipping: {money(order.total_shipping_fee)}',
            f'Total: {money(total)}',
            f'Notes: {order.notes or "-"}',
            '==============================',
            'Sign: ________',
        ]
    elif template == 'statement':
        title = '对账单'
        lines = [
            '======== STATEMENT ========',
            f'Order: {public_order_number}',
            f'Customer: {customer}',
            f'TG: {user.telegram_id if user else "-"}',
            f'Time: {order.created_at}',
            '---------------------------',
            'Details:',
        ]
        for group in grouped_rows:
            details = '、'.join(
                f"{row['product_name']}x{row['quantity']}={money(row['amount'])}"
                for row in group['flavors']
            )
            lines.append(f"- {group['category_name']}: {details}")
        lines += [
            '---------------------------',
            f'Products: {money(order.total_amount)}',
            f'Shipping: {money(order.total_shipping_fee)}',
            f'Total: {money(total)}',
            f'Paid: {money(paid)}',
            f'Due: {money(due)}',
            f'PayStatus: {pay_status}',
            f'UserDebt: {money(debt)}',
            '---------------------------',
            'Pay methods: CNY / USDT',
            '===========================',
        ]
    else:
        title = '订单文本'
        lines = [
            '======== ORDER ========',
            f'Order: {public_order_number}',
            f'Customer: {customer}',
            f'Status: {order.status} / {pay_status}',
            '-----------------------',
        ]
        for group in grouped_rows:
            lines.append(f"- {group['text']} {money(group['amount'])}")
        lines += [
            '-----------------------',
            f'Total: {money(total)} Paid: {money(paid)} Due: {money(due)}',
            f'To: {order.recipient_name} {order.recipient_phone}',
            f'Addr: {order.recipient_address}',
            '=======================',
        ]

    text = chr(10).join(lines)
    rows_html = ''.join([
        '<tr>'
        f"<td><b>{html.escape(str(r['category_name']))}</b>："
        + '、'.join(
            f"{html.escape(str(flavor['product_name']))}×{int(flavor['quantity'])}"
            for flavor in r['flavors']
        )
        + "</td>"
        f"<td>{html.escape('/'.join(r['warehouses']))}</td>"
        f"<td class='num'>{r['quantity']}</td>"
        f"<td class='num compact-price'>—</td>"
        f"<td class='num'>{money_html(r['amount'])}</td>"
        '</tr>'
        for r in grouped_rows
    ])
    debt_html = f"<div>客户总欠款: {money_html(debt)}</div>" if template == 'statement' else ''
    html_content = (
        '<div class="sheet">'
        f'<div class="head"><div class="title">{title}</div>'
        f'<div class="meta">订单号: {public_order_number}</div></div>'
        '<div class="grid">'
        f'<div><b>客户</b><br>{customer}</div>'
        f'<div><b>下单时间（北京时间）</b><br>{business_format(order.created_at)}</div>'
        f'<div><b>状态</b><br>{order.status} / {pay_status}</div>'
        f'<div><b>收件人</b><br>{order.recipient_name} {order.recipient_phone}</div>'
        '</div>'
        f'<div class="addr"><b>地址</b>: {order.recipient_address}</div>'
        '<table><thead><tr><th>商品</th><th>仓库</th><th>数量</th><th>单价</th><th>金额</th></tr></thead>'
        f'<tbody>{rows_html}</tbody></table>'
        '<div class="sums">'
        f'<div>商品合计: {money_html(order.total_amount)}</div>'
        f'<div>运费合计: {money_html(order.total_shipping_fee)}</div>'
        f'<div><b>应付总额: {money_html(total)}</b></div>'
        f'<div>已收: {money_html(paid)} | 待收: {money_html(due)}</div>'
        f'{debt_html}'
        '</div>'
        f'<div class="foot">备注: {order.notes or "-"}<br>人工收账, 不强制每单一结</div>'
        '</div>'
    )

    return {
        'order_id': order.id,
        'order_number': public_order_number,
        'internal_order_number': order.order_number,
        'template': template,
        'title': title,
        'print_text': text,
        'html': html_content,
        'data': {
            'customer': customer,
            'total': total,
            'paid': paid,
            'due': due,
            'debt': debt,
            'payment_status': pay_status,
            'items': item_rows,
            'grouped_items': grouped_rows,
        }
    }


@app.post("/api/orders/{order_id}/notify")
async def notify_order(
    order_id: int,
    event: str = Form('shipped'),
    _auth: str = Depends(require_admin_or_api_secret),
    session: Session = Depends(get_db_session)
):
    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail='订单不存在')
    user = order.user
    if not user:
        raise HTTPException(status_code=400, detail='订单无用户')
    event = (event or 'shipped').lower()
    if event == 'shipped':
        text_msg = build_shipping_notify_text(order, session)
    elif event == 'confirmed':
        text_msg = build_confirm_notify_text(order)
    else:
        raise HTTPException(status_code=400, detail='event only supports shipped/confirmed')
    from account_notifications import order_status_notice
    ok, detail = await order_status_notice(db, session, order, event, text_msg, send_telegram_message_async)
    if not ok:
        raise HTTPException(status_code=400, detail=f'notify failed: {detail}')
    return {'success': True, 'message': 'notified', 'event': event}


@app.get("/admin/finance", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_finance(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    request.session.setdefault('finance_csrf',secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "admin/finance.html", {'finance_csrf':request.session['finance_csrf']})


@app.get("/admin/daily", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_daily_page(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    return templates.TemplateResponse(request, "admin/stats.html", {})


@app.get("/admin/orders/print", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_order_print_page(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    return templates.TemplateResponse(request, "admin/order_print.html", {})





@app.post("/api/products/parents/{parent_id}/delete")
@_run_blocking_endpoint
def delete_parent_category(
    request: Request,
    parent_id: int,
    move_to_default: bool = Form(True),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """删除商品分类；默认将子商品迁移到默认分类。"""
    if _auth != 'session' or request.session.get('admin_username') != ADMIN_USERNAME:
        raise HTTPException(403,'分类删除仅允许管理员操作')
    expected=request.session.get('category_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Category-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面')
    from category_lifecycle import archive
    try:
        result=archive(db,parent_id,request.session['admin_username'])
    except ValueError as error:
        raise HTTPException(400,str(error)) from None
    if not result.get('success'):
        raise HTTPException(status_code=400, detail=result.get('error') or '删除失败')
    return result


@app.get('/api/products/category-delete-access')
@_run_blocking_endpoint
def category_delete_access(request: Request):
    if not is_admin_logged_in(request) or request.session.get('admin_username') != ADMIN_USERNAME:
        raise HTTPException(401,'请先以管理员登录')
    return {'csrf':request.session.setdefault('category_csrf',secrets.token_urlsafe(32))}


@app.post('/api/products/parents/{parent_id}/availability')
@_run_blocking_endpoint
def category_availability(request: Request, parent_id:int, is_active:bool=Form(...),
                          expected_active:bool=Form(...), session:Session=Depends(get_db_session),
                          _auth:str=Depends(require_admin_or_api_secret)):
    if _auth != 'session' or request.session.get('admin_username') != ADMIN_USERNAME:
        raise HTTPException(403,'仅管理员可切换分类接单状态')
    expected=request.session.get('category_csrf','')
    if not expected or not secrets.compare_digest(expected,request.headers.get('X-Category-CSRF','')):
        raise HTTPException(403,'请求校验失败，请刷新页面')
    from category_availability import set_status
    try:return set_status(session,parent_id,is_active,expected_active,request.session['admin_username'])
    except ValueError as error:raise HTTPException(409,str(error)) from None


@app.post("/api/products/parents/{parent_id}/batch_update")
@_run_blocking_endpoint
def batch_update_parent_products(
    parent_id: int,
    price_review_token: str = Form(''),
    cost_price: Optional[float] = Form(None),
    level1_agent_price: Optional[str] = Form(None),
    level2_agent_price: Optional[str] = Form(None),
    regular_price: Optional[str] = Form(None),
    low_stock_threshold: Optional[int] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """按大类同步修改其下所有口味价格/预警值。"""
    level1_agent_price,level2_agent_price,regular_price = map(_sale_form_amount,
        (level1_agent_price,level2_agent_price,regular_price))
    import math
    from catalog_retirement import require_operating
    require_operating(session, 'product', parent_id)
    parent = session.query(Product).filter(Product.id == parent_id, Product.is_parent == True).first()
    if not parent:
        raise HTTPException(status_code=404, detail="分类不存在")
    review_children = session.query(Product).filter(Product.parent_id == parent_id, Product.is_parent == False).all()
    __import__('sale_price_review').catalog_confirmation(session,[parent]+review_children,
        dict(level1_agent_price=level1_agent_price,level2_agent_price=level2_agent_price,regular_price=regular_price),
        price_review_token,dict(route='catalog_parent_batch',parent_id=parent_id))
    updates = {}
    for field, value in {
        "cost_price": cost_price,
        "level1_agent_price": level1_agent_price,
        "level2_agent_price": level2_agent_price,
        "regular_price": regular_price,
    }.items():
        if value is not None:
            if not math.isfinite(value) or value < 0:
                raise HTTPException(status_code=400, detail="价格必须为有限的非负数")
            updates[field] = float(value)
    if low_stock_threshold is not None:
        updates["low_stock_threshold"] = normalize_low_stock_threshold(low_stock_threshold)
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要同步的字段")

    merged = {
        "cost_price": updates.get("cost_price", parent.cost_price or 0),
        "level1_agent_price": updates.get("level1_agent_price", parent.level1_agent_price or 0),
        "level2_agent_price": updates.get("level2_agent_price", parent.level2_agent_price or 0),
        "regular_price": updates.get("regular_price", parent.regular_price or 0),
    }
    if not (merged["level1_agent_price"] <= merged["level2_agent_price"] <= merged["regular_price"]):
        raise HTTPException(status_code=400, detail="价格层级不正确：合伙人价 <= 超级VIP价 <= VIP客户价（售价允许低于成本）")

    for field, value in updates.items():
        setattr(parent, field, value)
    if 'cost_price' in updates: parent.reference_price_state='SET'
    children = session.query(Product).filter(Product.parent_id == parent_id, Product.is_parent == False).all()
    for child in children:
        for field, value in updates.items():
            setattr(child, field, value)
        if 'cost_price' in updates: child.reference_price_state='SET'
        if "low_stock_threshold" in updates:
            db.refresh_product_stock_status(session, child.id)
    session.commit()
    return {
        "success": True,
        "parent_id": parent_id,
        "parent_name": parent.name,
        "updated_fields": updates,
        "child_count": len(children),
        "message": f"已同步 {len(children)} 个口味",
    }


def _packaging_limits() -> dict:
    """当前自动分单容量；同地址和单次总支数不设上限。"""
    import config as cfg
    cfg.load_runtime_settings()
    limits = package_rules.normalize({
        'max_units_per_order': 0,
        'units_per_package': getattr(cfg, 'ORDER_AUTO_SPLIT_UNITS', 6),
        'address_daily_max_units': getattr(cfg,'ADDRESS_DAILY_MAX_UNITS',0),
    })
    limits['category_limits'] = package_rules.normalize_category_limits(
        getattr(cfg, 'CATEGORY_SPLIT_LIMITS', {})
    )
    limits['address_daily_max_orders'] = max(0,int(getattr(cfg,'ADDRESS_DAILY_MAX_ORDERS',0) or 0))
    return limits


def _address_daily_units(
    session: Session,
    address: str,
    exclude_order_id=None,
) -> dict:
    """同一收货地址「今天」已经下了多少支。

    「今天」按北京时间算（服务器是 UTC、created_at 存 utcnow），否则会错 8 小时，
    北京早上 7 点那一单会被算到前一天去，等于放行两轮。
    已取消的订单不计。地址用归一化后的键比对，不做模糊匹配。
    """
    key = package_rules.address_key(address)
    result = {
        'day': package_rules.local_day_str(),
        'address_key': key,
        'used_units': 0,
        'orders': 0,
    }
    if not key:
        return result
    start, end = package_rules.day_window_utc()
    rows = (
        session.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.created_at >= start, Order.created_at < end)
        .all()
    )
    used = 0
    orders = 0
    for order in rows:
        if exclude_order_id and int(order.id) == int(exclude_order_id):
            continue
        if str(order.status or '').lower() in ('cancelled', 'canceled', 'closed'):
            continue
        if package_rules.address_key(order.recipient_address) != key:
            continue
        orders += 1
        used += package_rules.total_units(order.items or [])
    result['used_units'] = used
    result['orders'] = orders
    return result


def _sales_pause_state(session: Session) -> dict:
    """停仓当前状态（按仓库）。GET / POST 共用，避免两处算法不一致。"""
    import config as cfg
    cfg.load_runtime_settings()
    section = sales_pause.normalize(getattr(cfg, 'SALES_PAUSE_SECTION', None))
    from catalog_retirement import operating_query
    warehouses = [
        {'id': w.id, 'name': w.name, 'is_active': bool(w.is_active)}
        for w in operating_query(session, Warehouse, 'warehouse').order_by(Warehouse.id).all()
    ]
    rows = sales_pause.describe(section, warehouses)
    return {
        # 旧字段保留：老前端/脚本只读这三个也不会坏。paused 现在表示「全部仓库都停」
        'paused': sales_pause.all_paused(section, warehouses),
        'message': sales_pause.message_for(
            section, None, getattr(cfg, 'SALES_PAUSED_MESSAGE', '')
        ),
        'paused_at': section.get('paused_at') or '',
        'all_paused': sales_pause.all_paused(section, warehouses),
        'any_paused': any(row['paused'] for row in rows),
        'warehouses': rows,
    }


@app.get("/api/settings/sales_pause")
@_run_blocking_endpoint
def get_sales_pause_settings(
    request: Request,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """停仓当前状态：全局派生位 + 每个仓库一行。"""
    return _sales_pause_state(session)


@app.post("/api/settings/sales_pause")
@_run_blocking_endpoint
def update_sales_pause_settings(
    request: Request,
    paused: str = Form(...),
    message: Optional[str] = Form(None),
    warehouse_id: Optional[str] = Form(None),
    message_only: Optional[str] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """一键停仓 / 开仓。写入 settings.json，Bot 在下一次下单前会自动读到。

    只拦「新订单进入」，不影响已生成订单的发货、打单和售后。
    """
    import json
    from datetime import datetime as _dt
    from pathlib import Path as _P
    import config as cfg


    want_paused = str(paused).strip().lower() in ('1', 'true', 'yes', 'on')
    settings_file = _P('settings.json')
    data = {}
    if settings_file.exists():
        try:
            data = json.loads(settings_file.read_text(encoding='utf-8'))
        except Exception:
            data = {}

    from catalog_retirement import operating_query
    warehouses = [
        {'id': w.id, 'name': w.name, 'is_active': bool(w.is_active)}
        for w in operating_query(session, Warehouse, 'warehouse').order_by(Warehouse.id).all()
    ]
    # warehouse_id 不传 / 传空 / 传 all 都表示「全部仓库」，保持旧的一键停仓语义
    raw_target = str(warehouse_id or '').strip()
    if not raw_target or raw_target.lower() == 'all':
        targets = None
    else:
        targets = [x.strip() for x in raw_target.split(',') if x.strip()]
        known = {str(w['id']) for w in warehouses}
        unknown = [x for x in targets if x not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"仓库不存在: {'、'.join(unknown)}")

    text = (message or '').strip() or (data.get('sales') or {}).get('message') or getattr(
        cfg, 'SALES_PAUSED_MESSAGE', ''
    )
    if str(message_only or '').strip().lower() in ('1', 'true', 'yes', 'on'):
        # 只保存提示语。绝不能顺手写开关：部分停仓时那会把所有仓库一起开/停。
        section = sales_pause.normalize(data.get('sales'))
        section['message'] = text
        data['sales'] = section
    else:
        data['sales'] = sales_pause.apply_pause(
            data.get('sales'),
            warehouses,
            want_paused,
            warehouse_ids=targets,
            message=text,
            now=_dt.utcnow().isoformat(timespec='seconds')+'Z',
        )
    settings_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    cfg.load_runtime_settings()
    _load_runtime_settings()
    state = _sales_pause_state(session)
    state['success'] = True
    return state


@app.get("/api/settings/packaging")
@_run_blocking_endpoint
def get_packaging_settings(
    request: Request,
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """数量/包裹限额当前值。"""
    limits = _packaging_limits()
    warehouses = {
        # Disabled is not unbound: preserve identity for existing category rules.
        # This read-only display mapping does not change order acceptance.
        int(row.id): row.name if row.is_active else f'{row.name}（停用）'
        for row in session.query(Warehouse).all()
    }
    categories = session.query(Product).filter(
        Product.is_parent == True,
        Product.is_active == True,
        Product.is_archived == False,
    ).order_by(Product.default_warehouse_id.asc(), Product.category_serial.asc(), Product.id.asc()).all()
    return {
        **limits,
        'default_units_per_order': limits['units_per_package'],
        'categories': [{
            'id': int(category.id),
            'name': category.name,
            'category_display_id': _display_serial('C', category.category_serial),
            'warehouse_id': category.default_warehouse_id,
            'warehouse_name': warehouses.get(int(category.default_warehouse_id or 0), '未绑定仓库'),
            'limit': int(limits['category_limits'].get(str(category.id), limits['units_per_package'])),
            'is_custom': str(category.id) in limits['category_limits'],
        } for category in categories],
        'warehouses': [{'id': warehouse_id, 'name': name} for warehouse_id, name in warehouses.items()],
        'description': package_rules.describe(limits),
        'day': package_rules.local_day_str(),
    }


@app.post("/api/settings/packaging")
@_run_blocking_endpoint
def update_packaging_settings(
    request: Request,
    max_units_per_order: Optional[int] = Form(None),
    units_per_package: Optional[int] = Form(None),
    address_daily_max_units: Optional[int] = Form(None),
    address_daily_max_orders: Optional[int] = Form(None),
    category_limits: Optional[str] = Form(None),
    session: Session = Depends(get_db_session),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """保存自动分单容量；总支数和同地址当日数量固定不限制。"""
    import json
    from pathlib import Path as _P
    import config as cfg

    existing = {}
    settings_file = _P('settings.json')
    data = {}
    if settings_file.exists():
        try:
            data = json.loads(settings_file.read_text(encoding='utf-8'))
            existing = data.get('packaging') or {}
        except Exception:
            data = {}
            existing = {}
    capacity = max(1, int(units_per_package or existing.get('units_per_package') or 6))
    normalized_category_limits = package_rules.normalize_category_limits(existing.get('category_limits'))
    if category_limits is not None:
        try:
            requested_limits = json.loads(category_limits or '{}')
        except Exception:
            raise HTTPException(status_code=400, detail='品类分单规则格式不正确')
        normalized_category_limits = package_rules.normalize_category_limits(requested_limits)
        valid_parent_ids = {
            str(row[0]) for row in session.query(Product.id).filter(
                Product.is_parent == True,
                Product.is_archived == False,
            ).all()
        }
        invalid_ids = sorted(set(normalized_category_limits) - valid_parent_ids)
        if invalid_ids:
            raise HTTPException(status_code=400, detail='存在无效品类：' + ','.join(invalid_ids))
    daily_orders=max(0,int(address_daily_max_orders if isinstance(address_daily_max_orders,int) else existing.get('address_daily_max_orders') or 0))
    if daily_orders:
        from address_daily_orders import ready as address_orders_ready
        if not address_orders_ready(session):
            raise HTTPException(status_code=400,detail='同地址每日订单数Migration未完成，请先完成发布验收')
    values = {
        'max_units_per_order': 0,
        'units_per_package': capacity,
        'address_daily_max_units': max(0,int(address_daily_max_units if isinstance(address_daily_max_units,int) else existing.get('address_daily_max_units') or 0)),
        'address_daily_max_orders': daily_orders,
        'category_limits': normalized_category_limits,
    }
    data['packaging'] = values
    settings_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    # 环境级默认值仍是权威容量；当前进程同步更新，下一单立即生效。
    cfg.ORDER_AUTO_SPLIT_UNITS = capacity
    cfg.load_runtime_settings()
    cfg.ORDER_AUTO_SPLIT_UNITS = capacity
    cfg.UNITS_PER_PACKAGE = capacity
    _load_runtime_settings()
    limits = _packaging_limits()
    return {
        'success': True,
        **limits,
        'default_units_per_order': limits['units_per_package'],
        'description': package_rules.describe(limits),
    }


@app.get("/api/settings/shipping")
@_run_blocking_endpoint
def get_shipping_settings(
    request: Request,
    _auth: str = Depends(require_admin_or_api_secret),
):
    import config as cfg
    return {
        'mode': cfg.SHIPPING_MODE,
        'fee_amount': cfg.SHIPPING_FEE_AMOUNT,
        'normal_fee_amount': getattr(cfg, 'SHIPPING_NORMAL_FEE_AMOUNT', cfg.SHIPPING_FEE_AMOUNT),
        'pod_fee_amount': getattr(cfg, 'SHIPPING_POD_FEE_AMOUNT', cfg.SHIPPING_FEE_AMOUNT),
        'normal_qty': cfg.SHIPPING_NORMAL_QTY,
        'pod_qty': cfg.SHIPPING_POD_QTY,
        'pod_keywords': cfg.SHIPPING_POD_KEYWORDS,
        'rule_text': (
            f"普通商品每{cfg.SHIPPING_NORMAL_QTY}只/根 "
            f"{getattr(cfg, 'SHIPPING_NORMAL_FEE_AMOUNT', cfg.SHIPPING_FEE_AMOUNT):g} 元；"
            f"烟弹类每{cfg.SHIPPING_POD_QTY}颗 "
            f"{getattr(cfg, 'SHIPPING_POD_FEE_AMOUNT', cfg.SHIPPING_FEE_AMOUNT):g} 元"
        ),
    }


@app.post("/api/settings/shipping")
@_run_blocking_endpoint
def update_shipping_settings(
    request: Request,
    mode: str = Form('qty_rule'),
    fee_amount: float = Form(6),
    normal_fee_amount: Optional[float] = Form(None),
    pod_fee_amount: Optional[float] = Form(None),
    normal_qty: Optional[int] = Form(None),
    pod_qty: Optional[int] = Form(None),
    pod_keywords: str = Form('烟弹,弹,pod,POD'),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """保存运费设置到本地 settings.json，重启后仍生效。"""
    import json
    import config as cfg
    from pathlib import Path as _P
    settings_file = _P('settings.json')
    data = {}
    if settings_file.exists():
        try:
            data = json.loads(settings_file.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    normal_fee = float(normal_fee_amount if normal_fee_amount is not None else fee_amount)
    pod_fee = float(pod_fee_amount if pod_fee_amount is not None else fee_amount)
    data['shipping'] = {
        'mode': mode,
        'fee_amount': float(fee_amount),
        'normal_fee_amount': normal_fee,
        'pod_fee_amount': pod_fee,
        'normal_qty': int(normal_qty if normal_qty is not None else cfg.SHIPPING_NORMAL_QTY),
        'pod_qty': int(pod_qty if pod_qty is not None else cfg.SHIPPING_POD_QTY),
        'pod_keywords': [x.strip() for x in str(pod_keywords).split(',') if x.strip()],
    }
    # 风火递已从彩虹仓退役。既有设置段原样保留作审计/回滚参考，
    # 但此接口不再返回、新增或修改任何风火递配置。
    settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    # hot apply to module globals
    cfg.SHIPPING_MODE = data['shipping']['mode']
    cfg.SHIPPING_FEE_AMOUNT = data['shipping']['fee_amount']
    cfg.SHIPPING_NORMAL_FEE_AMOUNT = data['shipping']['normal_fee_amount']
    cfg.SHIPPING_POD_FEE_AMOUNT = data['shipping']['pod_fee_amount']
    cfg.SHIPPING_NORMAL_QTY = data['shipping']['normal_qty']
    cfg.SHIPPING_POD_QTY = data['shipping']['pod_qty']
    cfg.SHIPPING_POD_KEYWORDS = data['shipping']['pod_keywords']
    return {'success': True, 'settings': {'shipping': data['shipping']}}


def _load_runtime_settings():
    import json
    from pathlib import Path as _P
    import config as cfg
    p = _P('settings.json')
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return
    shipping = data.get('shipping') or {}
    if shipping:
        cfg.SHIPPING_MODE = shipping.get('mode', cfg.SHIPPING_MODE)
        cfg.SHIPPING_FEE_AMOUNT = float(shipping.get('fee_amount', cfg.SHIPPING_FEE_AMOUNT))
        cfg.SHIPPING_NORMAL_FEE_AMOUNT = float(shipping.get(
            'normal_fee_amount',
            getattr(cfg, 'SHIPPING_NORMAL_FEE_AMOUNT', cfg.SHIPPING_FEE_AMOUNT),
        ))
        cfg.SHIPPING_POD_FEE_AMOUNT = float(shipping.get(
            'pod_fee_amount',
            getattr(cfg, 'SHIPPING_POD_FEE_AMOUNT', cfg.SHIPPING_FEE_AMOUNT),
        ))
        cfg.SHIPPING_NORMAL_QTY = int(shipping.get('normal_qty', cfg.SHIPPING_NORMAL_QTY))
        cfg.SHIPPING_POD_QTY = int(shipping.get('pod_qty', cfg.SHIPPING_POD_QTY))
        if shipping.get('pod_keywords'):
            cfg.SHIPPING_POD_KEYWORDS = shipping.get('pod_keywords')
def _raise_retired_fenghuodi():
    """Fail closed before parsing provider input or touching business state."""
    raise HTTPException(status_code=410, detail='风火递功能已从彩虹仓退役')


def build_fenghuodi_data(order_id: int, session: Session):
    _raise_retired_fenghuodi()
    import urllib.parse
    import config as cfg
    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        return None
    items = []
    for it in order.items:
        pname = _display_order_item_name(it)
        items.append(f"{pname}x{it.quantity}")
    items_text = ','.join(items)
    address_parse = normalize_cn_address(order.recipient_address or '')
    normalized_address = address_parse.get('normalized_address') or order.recipient_address or ''
    mapping = {
        'order_id': str(order.id),
        'order_number': order.order_number or '',
        'name': order.recipient_name or '',
        'phone': order.recipient_phone or '',
        'address': normalized_address,
        'raw_address': order.recipient_address or '',
        'items': items_text,
        'amount': f"{float(order.grand_total or 0):.2f}",
        'sender_name': cfg.FENGHUODI_SENDER_NAME or '',
        'sender_phone': cfg.FENGHUODI_SENDER_PHONE or '',
        'sender_address': cfg.FENGHUODI_SENDER_ADDRESS or '',
    }
    enc = {k: urllib.parse.quote(str(v), safe='') for k, v in mapping.items()}
    url_tpl = cfg.FENGHUODI_PRINT_URL or ''
    try:
        print_url = url_tpl.format(**enc)
    except Exception:
        print_url = url_tpl
    payload = {
        'orderNo': mapping['order_number'],
        'receiver': {
            'name': mapping['name'],
            'mobile': mapping['phone'],
            'address': mapping['address'],
            'raw_address': mapping['raw_address'],
            'province': address_parse.get('province') or '',
            'city': address_parse.get('city') or '',
            'district': address_parse.get('district') or '',
            'town': address_parse.get('town') or '',
            'detail': address_parse.get('detail') or '',
            'address_parse': address_parse,
        },
        'sender': {
            'name': mapping['sender_name'],
            'mobile': mapping['sender_phone'],
            'address': mapping['sender_address'],
        },
        'goods': items_text,
        'amount': mapping['amount'],
    }
    return {
        'enabled': bool(cfg.FENGHUODI_ENABLED),
        'print_url': print_url,
        'api_url': cfg.FENGHUODI_API_URL,
        'payload': payload,
        'order': {
            'id': order.id,
            'order_number': order.order_number,
            'recipient_name': order.recipient_name,
            'recipient_phone': order.recipient_phone,
            'recipient_address': order.recipient_address,
            'recipient_address_normalized': normalized_address,
            'address_parse': address_parse,
            'grand_total': float(order.grand_total or 0),
            'items': items,
        }
    }


@app.get("/api/orders/{order_id}/fenghuodi")
@_run_blocking_endpoint
def build_fenghuodi_link(
    order_id: int,
    _auth: str = Depends(require_admin_or_api_secret),
):
    _raise_retired_fenghuodi()
    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail='订单不存在')
    if order.status == 'split_pending':
        raise HTTPException(status_code=409, detail='分单订单必须先人工确认，确认前不能推送风火递')
    data = build_fenghuodi_data(order_id, session)
    return data




def _fhd_client_from_config():
    _raise_retired_fenghuodi()
    import config as cfg
    from fhd_client import FengHuoDiClient
    return FengHuoDiClient(
        pid=getattr(cfg, 'FENGHUODI_PID', '') or '',
        psecret=getattr(cfg, 'FENGHUODI_PSECRET', '') or '',
        user_id=getattr(cfg, 'FENGHUODI_USER_ID', '') or '',
        base_url=getattr(cfg, 'FENGHUODI_BASE_URL', 'https://openapi.fhd001.com') or 'https://openapi.fhd001.com',
        sender={
            'name': getattr(cfg, 'FENGHUODI_SENDER_NAME', '') or '',
            'mobile': getattr(cfg, 'FENGHUODI_SENDER_PHONE', '') or '',
            'phone': getattr(cfg, 'FENGHUODI_SENDER_PHONE', '') or '',
            'address': getattr(cfg, 'FENGHUODI_SENDER_ADDRESS', '') or '',
        },
    )


def _safe_json_dumps(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _extract_fhd_shipping_info(result: dict) -> dict:
    response = (result or {}).get('response') or {}
    data = response.get('data') if isinstance(response, dict) else {}
    obj = response.get('object') if isinstance(response, dict) else {}
    candidates = []
    for item in (data, obj, response):
        if isinstance(item, dict):
            candidates.append(item)
    if isinstance(data, list):
        candidates.extend([x for x in data if isinstance(x, dict)])

    info = {'waybill_code': '', 'waybill_codes': [], 'cp_code': '', 'cp_name': ''}
    for item in candidates:
        for key in ('waybillCode', 'wlbCode', 'waybill', 'mailNo', 'waybillCodeList'):
            for code in _waybill_values(item.get(key)):
                if code not in info['waybill_codes']:
                    info['waybill_codes'].append(code)
        info['cp_code'] = info['cp_code'] or str(item.get('cpCode') or item.get('logisticsCode') or '')
        info['cp_name'] = info['cp_name'] or str(item.get('cpName') or item.get('logisticsName') or '')
    info['waybill_code'] = info['waybill_codes'][0] if info['waybill_codes'] else ''
    return info


def _summarize_fhd_error(result: dict, fallback: str = '') -> str:
    response = (result or {}).get('response') or {}
    if isinstance(response, dict):
        msg = response.get('errorMsg') or response.get('message') or response.get('msg') or ''
        code = response.get('errorCodeFinal') or response.get('errorCode') or response.get('scode') or ''
        if msg or code:
            return f"{code} {msg}".strip()
    return fallback or _safe_json_dumps(result)[:500]


def _record_fhd_push_result(
    session: Session,
    order: Order,
    *,
    split_mode: str,
    split_key: str,
    mode: str,
    result: dict,
    success: bool,
    error_message: str = '',
):
    _raise_retired_fenghuodi()
    info = _extract_fhd_shipping_info(result)
    trade_id = ''
    try:
        trade_id = str(((result or {}).get('request') or {}).get('params', {}).get('trade', {}).get('tradeId') or '')
    except Exception:
        trade_id = ''
    if not trade_id:
        trade_id = order.order_number

    waybill_codes = info.get('waybill_codes') or _waybill_values(info.get('waybill_code'))
    # Keep the first code on the canonical row and add one row per additional
    # package. This preserves every code even when a provider returns enough
    # values to exceed the column's 100-character limit.
    waybill_value = (waybill_codes[0] if waybill_codes else '')[:100]
    log = FenghuodiPushLog(
        order_id=order.id,
        order_number=order.order_number,
        split_mode=split_mode or 'none',
        split_key=split_key or '',
        mode=mode,
        status='success' if success else 'failed',
        trade_id=trade_id,
        waybill_code=waybill_value,
        cp_code=info.get('cp_code') or '',
        cp_name=info.get('cp_name') or '',
        request_payload=_safe_json_dumps((result or {}).get('request') or {}),
        response_payload=_safe_json_dumps((result or {}).get('response') or result or {}),
        error_message='' if success else (error_message or _summarize_fhd_error(result)),
    )
    session.add(log)
    for index, code in enumerate(waybill_codes[1:], start=2):
        session.add(FenghuodiPushLog(
            order_id=order.id,
            order_number=order.order_number,
            split_mode=split_mode or 'none',
            split_key=f'{split_key}#{index}',
            mode=mode,
            status='success' if success else 'failed',
            trade_id=trade_id,
            waybill_code=str(code)[:100],
            cp_code=info.get('cp_code') or '',
            cp_name=info.get('cp_name') or '',
            request_payload=_safe_json_dumps((result or {}).get('request') or {}),
            response_payload=_safe_json_dumps((result or {}).get('response') or result or {}),
            error_message='' if success else (error_message or _summarize_fhd_error(result)),
        ))
    order.fhd_push_count = int(getattr(order, 'fhd_push_count', 0) or 0) + 1
    order.fhd_last_push_at = datetime.utcnow()
    order.fhd_push_mode = mode
    order.fhd_last_error = '' if success else log.error_message
    if success:
        order.fhd_push_status = 'success'
        order.fhd_trade_id = trade_id
        if waybill_codes:
            # The canonical Order field is kept for backwards compatibility;
            # complete package data remains in FenghuodiPushLog.
            order.fhd_waybill_code = waybill_codes[0]
        if info.get('cp_code'):
            order.fhd_cp_code = info['cp_code']
        if info.get('cp_name'):
            order.fhd_cp_name = info['cp_name']
    else:
        order.fhd_push_status = 'failed'
    session.commit()
    session.refresh(order)
    return log


def _push_fhd_part(client, order: Order, *, items=None, split_mode: str = 'none', split_key: str = '', suffix: str = ''):
    _raise_retired_fenghuodi()
    import config as cfg
    merchant_id = getattr(cfg, 'FENGHUODI_MERCHANT_ID', '') or ''
    user_id = getattr(cfg, 'FENGHUODI_USER_ID', '') or ''
    if merchant_id:
        result = client.push_local_merchant_trade(order, merchant_id=merchant_id, items=items, trade_id_suffix=suffix)
        if result.get('success'):
            return 'openapi_pushMerchantTrade', result
        if not user_id:
            return 'openapi_pushMerchantTrade', result
    result = client.push_local_order(order, user_id=user_id or None, items=items, order_id_suffix=suffix)
    return 'openapi_pushOrders', result


async def _push_fhd_part_async(
    client,
    order: Order,
    *,
    items=None,
    split_mode: str = 'none',
    split_key: str = '',
    suffix: str = '',
):
    """Execute one synchronous FHD request in a bounded worker thread."""
    _raise_retired_fenghuodi()
    started = time.monotonic()
    try:
        async with _get_fhd_push_semaphore():
            mode, result = await asyncio.wait_for(
                asyncio.to_thread(
                    _push_fhd_part,
                    client,
                    order,
                    items=items,
                    split_mode=split_mode,
                    split_key=split_key,
                    suffix=suffix,
                ),
                timeout=_FHD_PUSH_TIMEOUT_SECONDS,
            )
            return mode, result
    except asyncio.TimeoutError:
        logger.warning(
            'FHD push timed out order=%s split=%s elapsed=%.3fs',
            getattr(order, 'id', None), split_key, time.monotonic() - started,
        )
        return 'openapi_timeout', {
            'success': False,
            'timeout': True,
            'response': {'errorCode': 'TIMEOUT', 'errorMsg': '风火递请求超时'},
        }
    finally:
        logger.info(
            'FHD push finished order=%s split=%s elapsed=%.3fs',
            getattr(order, 'id', None), split_key, time.monotonic() - started,
        )


@app.get("/api/fenghuodi/auth_url")
@_run_blocking_endpoint
def fhd_auth_url(_auth: str = Depends(require_admin_or_api_secret)):
    _raise_retired_fenghuodi()
    import config as cfg
    client = _fhd_client_from_config()
    if not client.configured:
        raise HTTPException(status_code=400, detail='请先配置风火递 PID / PSECRET')
    url = client.merchant_auth_url(
        merchant_id=getattr(cfg, 'FENGHUODI_MERCHANT_ID', 'default') or 'default',
        merchant_name=getattr(cfg, 'FENGHUODI_MERCHANT_NAME', 'TG订单商城') or 'TG订单商城',
    )
    return {'auth_url': url, 'pid': cfg.FENGHUODI_PID, 'merchant_id': cfg.FENGHUODI_MERCHANT_ID}


@app.get("/api/fenghuodi/user_auth_url")
@_run_blocking_endpoint
def fhd_user_auth_url(
    user_id: str = Query('', alias='user_id'),
    user_name: str = Query('', alias='user_name'),
    _auth: str = Depends(require_admin_or_api_secret),
):
    _raise_retired_fenghuodi()
    import config as cfg
    client = _fhd_client_from_config()
    if not client.configured:
        raise HTTPException(status_code=400, detail='请先配置风火递 PID / PSECRET')
    uid = (user_id or getattr(cfg, 'FENGHUODI_USER_ID', '') or getattr(cfg, 'FENGHUODI_MERCHANT_ID', '') or 'tg_shop_admin').strip()
    uname = (user_name or getattr(cfg, 'FENGHUODI_MERCHANT_NAME', '') or 'TG订单商城').strip()
    url = client.user_auth_url(user_id=uid, user_name=uname)
    return {'auth_url': url, 'pid': cfg.FENGHUODI_PID, 'user_id': uid, 'user_name': uname}


@app.get("/api/fenghuodi/logistics")
@_run_blocking_endpoint
def fhd_logistics(_auth: str = Depends(require_admin_or_api_secret)):
    _raise_retired_fenghuodi()
    client = _fhd_client_from_config()
    try:
        return client.get_logistics_companies()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/fenghuodi/merchants")
@_run_blocking_endpoint
def fhd_merchants(_auth: str = Depends(require_admin_or_api_secret)):
    _raise_retired_fenghuodi()
    client = _fhd_client_from_config()
    try:
        return client.get_merchants()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/fenghuodi/auth_users")
@_run_blocking_endpoint
def fhd_auth_users(_auth: str = Depends(require_admin_or_api_secret)):
    _raise_retired_fenghuodi()
    client = _fhd_client_from_config()
    try:
        return client.get_auth_users()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/orders/{order_id}/fenghuodi/push")
async def push_fenghuodi(
    order_id: int,
    _auth: str = Depends(require_admin_or_api_secret),
):
    """退役墓碑：禁止新的风火递推送。"""
    _raise_retired_fenghuodi()
    import config as cfg
    order = session.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail='订单不存在')
    if order.status == 'split_pending':
        raise HTTPException(status_code=409, detail='分单订单必须先人工确认，确认前不能推送风火递')
    if not getattr(cfg, 'FENGHUODI_ENABLED', True):
        raise HTTPException(status_code=400, detail='未启用风火递打印')
    client = _fhd_client_from_config()
    # Validate the normalized recipient address before any third-party call.
    # Fenghuodi/PDD rejects an empty receiverArea with error 2030; returning a
    # clear 400 here lets the operator fix the address instead of retrying a
    # malformed trade.
    try:
        from fhd_client import validate_recipient_address

        validate_recipient_address(order.recipient_address or '')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not client.configured:
        # 兼容旧模式：仅返回客户端链接
        data = build_fenghuodi_data(order_id, session)
        return {
            'success': True,
            'mode': 'client_link',
            'message': '未配置官方 PID/PSECRET，已回退到链接模式',
            'print_url': (data or {}).get('print_url'),
            'payload': (data or {}).get('payload'),
        }
    split_mode = (split_mode or 'none').strip().lower()
    if split_mode not in ('none', 'warehouse', 'user', 'qty'):
        raise HTTPException(
            status_code=400, detail='split_mode 只支持 none / warehouse / user / qty',
        )
    try:
        parts = []
        if split_mode == 'warehouse':
            by_warehouse = {}
            for item in order.items:
                key = item.warehouse_id or 0
                by_warehouse.setdefault(key, []).append(item)
            for warehouse_id, items in by_warehouse.items():
                warehouse_name = items[0].warehouse.name if items and items[0].warehouse else str(warehouse_id)
                parts.append({
                    'items': items,
                    'split_key': warehouse_name,
                    'suffix': f'-W{warehouse_id}',
                })
        elif split_mode == 'qty':
            # 每 N 支一个包裹，一个包裹一个运单号。数量不够拆时退回整单一个号，
            # orderId 不加后缀，行为和以前完全一样。
            size = int(_packaging_limits().get('units_per_package') or 0)
            groups = package_rules.split_into_packages(list(order.items or []), size)
            if len(groups) <= 1:
                parts.append({'items': None, 'split_key': '1/1', 'suffix': ''})
            else:
                for index, group in enumerate(groups, start=1):
                    parts.append({
                        'items': group,
                        'split_key': f'包裹{index}/{len(groups)}',
                        'suffix': f'-P{index}',
                    })
        else:
            split_key = ''
            if split_mode == 'user':
                split_key = order.user.display_name or order.user.username or order.user.telegram_id if order.user else str(order.user_id)
            parts.append({'items': None, 'split_key': split_key, 'suffix': ''})

        results = []
        success_count = 0
        for part in parts:
            mode, result = await _push_fhd_part_async(
                client,
                order,
                items=part['items'],
                split_mode=split_mode,
                split_key=part['split_key'],
                suffix=part['suffix'],
            )
            ok = bool(result.get('success'))
            if ok:
                success_count += 1
            log = _record_fhd_push_result(
                session,
                order,
                split_mode=split_mode,
                split_key=part['split_key'],
                mode=mode,
                result=result,
                success=ok,
                error_message='' if ok else _summarize_fhd_error(result, '风火递返回失败'),
            )
            results.append({
                'success': ok,
                'mode': mode,
                'split_key': part['split_key'],
                'log_id': log.id,
                'result': result,
            })
    except Exception as e:
        order.fhd_push_status = 'failed'
        order.fhd_last_error = str(e)
        order.fhd_last_push_at = datetime.utcnow()
        order.fhd_push_count = int(getattr(order, 'fhd_push_count', 0) or 0) + 1
        session.add(FenghuodiPushLog(
            order_id=order.id,
            order_number=order.order_number,
            split_mode=split_mode,
            split_key='',
            mode='openapi',
            status='failed',
            error_message=str(e),
        ))
        session.commit()
        raise HTTPException(status_code=502, detail=f'推送风火递失败: {e}')
    if len(results) > 1 and 0 < success_count < len(results):
        order.fhd_push_status = 'partial'
        session.commit()
    return {
        'success': success_count == len(results),
        'mode': results[0]['mode'] if len(results) == 1 else 'split_push',
        'split_mode': split_mode,
        'message': '已推送到风火递' if success_count == len(results) else f'风火递推送部分或全部失败：{success_count}/{len(results)} 成功',
        'results': results,
        'result': results[0]['result'] if len(results) == 1 else None,
        'order_number': order.order_number,
    }


@app.post("/api/fenghuodi/push_batch")
async def push_fenghuodi_batch(
    _auth: str = Depends(require_admin_or_api_secret),
):
    _raise_retired_fenghuodi()
    ids = []
    for part in (order_ids or '').replace('，', ',').split(','):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        raise HTTPException(status_code=400, detail='请选择订单')

    pending_split_ids = [
        row[0] for row in session.query(Order.id, Order.status).filter(
            Order.id.in_(ids), Order.status == 'split_pending',
        ).all()
    ]
    if pending_split_ids:
        raise HTTPException(status_code=409, detail={
            'code': 'SPLIT_ORDER_NOT_CONFIRMED',
            'message': '分单订单必须先人工确认，确认前不能批量推送风火递',
            'order_ids': pending_split_ids,
        })

    # Queue the whole batch and release the request/DB session immediately.
    # A detached task owns a fresh SQLAlchemy session and still records each
    # per-order result in the normal FHD log table.
    ids = ids[:100]
    job_id = uuid.uuid4().hex
    _fhd_batch_jobs[job_id] = {'status': 'queued', 'total': len(ids), 'completed': 0}
    task = asyncio.create_task(_run_fhd_batch_job(job_id, ids, split_mode, _auth or ''))
    _fhd_batch_tasks.add(task)
    task.add_done_callback(_fhd_batch_tasks.discard)
    return {
        'success': True,
        'queued': True,
        'job_id': job_id,
        'split_mode': split_mode,
        'total': len(ids),
        'message': f'已加入风火递后台队列（{len(ids)} 单）',
    }

    grouped = {}
    results = []
    for oid in ids[:100]:
        order = session.query(Order).filter(Order.id == oid).first()
        if not order:
            results.append({'order_id': oid, 'success': False, 'message': '订单不存在'})
            continue
        try:
            resp = await push_fenghuodi(order.id, split_mode=split_mode, session=session, _auth=_auth)
        except HTTPException as e:
            results.append({'order_id': oid, 'order_number': order.order_number, 'success': False, 'message': e.detail})
            continue
        group_key = '全部'
        if split_mode == 'user':
            group_key = order.user.display_name or order.user.username or order.user.telegram_id if order.user else str(order.user_id)
        elif split_mode == 'warehouse':
            names = sorted({item.warehouse.name for item in order.items if item.warehouse})
            group_key = ','.join(names) or '未分仓'
        elif split_mode == 'qty':
            _units = package_rules.total_units(order.items or [])
            _size = int(_packaging_limits().get('units_per_package') or 0)
            group_key = f'{package_rules.package_count(_units, _size)} 个包裹'
        grouped.setdefault(group_key, 0)
        grouped[group_key] += 1
        results.append({'order_id': order.id, 'order_number': order.order_number, **resp})
    return {
        'success': all(r.get('success') for r in results),
        'split_mode': split_mode,
        'groups': grouped,
        'results': results,
    }


def _upsert_fhd_callback_logs(
    session: Session,
    order: Order,
    trade_ids: List[str],
    waybills: List[str],
    *,
    cp_code: str = '',
    cp_name: str = '',
):
    _raise_retired_fenghuodi()
    """Attach callback package numbers to push logs without duplicating retries."""
    if not waybills:
        return
    # A callback may be rendered immediately after this function; invalidate
    # the per-session export/notification cache so newly attached codes appear.
    try:
        delattr(session, '_fhd_waybill_cache')
    except AttributeError:
        pass
    logs = []
    try:
        logs = (
            session.query(FenghuodiPushLog)
            .filter(FenghuodiPushLog.order_id == order.id)
            .order_by(FenghuodiPushLog.id.asc())
            .all()
        )
    except Exception:
        logs = []
    matched = [row for row in logs if not trade_ids or getattr(row, 'trade_id', None) in trade_ids]
    if not matched:
        matched = [row for row in logs if not getattr(row, 'waybill_code', None)]
    for index, code in enumerate(waybills):
        target = matched[index] if index < len(matched) else None
        if target is not None:
            existing = _waybill_values(getattr(target, 'waybill_code', None))
            if code not in existing:
                target.waybill_code = ','.join(existing + [code])[:100]
            target.status = 'success'
            if cp_code:
                target.cp_code = cp_code
            if cp_name:
                target.cp_name = cp_name
            continue
        session.add(FenghuodiPushLog(
            order_id=order.id,
            order_number=order.order_number,
            split_mode='callback',
            split_key=f'package-{index + 1}',
            mode='callback',
            status='success',
            trade_id=trade_ids[index] if index < len(trade_ids) else (trade_ids[0] if trade_ids else order.order_number),
            waybill_code=code[:100],
            cp_code=cp_code or '',
            cp_name=cp_name or '',
        ))


@app.get("/api/fenghuodi/batch_jobs/{job_id}")
@_run_blocking_endpoint
def fhd_batch_job_status(
    job_id: str,
    _auth: str = Depends(require_admin_or_api_secret),
):
    _raise_retired_fenghuodi()
    job = _fhd_batch_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='批量任务不存在或已过期')
    return {'job_id': job_id, **job}


@app.get("/api/fenghuodi/push_logs")
@_run_blocking_endpoint
def fhd_push_logs(
    _auth: str = Depends(require_admin_or_api_secret),
):
    _raise_retired_fenghuodi()
    query = session.query(FenghuodiPushLog)
    if status:
        query = query.filter(FenghuodiPushLog.status == status)
    if order_id:
        query = query.filter(FenghuodiPushLog.order_id == order_id)
    rows = query.order_by(FenghuodiPushLog.created_at.desc()).limit(min(int(limit or 200), 500)).all()
    return [{
        'id': row.id,
        'order_id': row.order_id,
        'order_number': row.order_number,
        'split_mode': row.split_mode,
        'split_key': row.split_key,
        'mode': row.mode,
        'status': row.status,
        'trade_id': row.trade_id,
        'waybill_code': row.waybill_code,
        'cp_code': row.cp_code,
        'cp_name': row.cp_name,
        'error_message': row.error_message,
        'response_payload': row.response_payload,
        'created_at': row.created_at.isoformat() if row.created_at else None,
    } for row in rows]


@app.post("/api/fenghuodi/callback/ship")
async def fhd_ship_callback(request: Request):
    """
    风火递发货回调（订单发货接口对接文档）。
    配置到开放平台后，打印/发货完成会回调这里。
    """
    _raise_retired_fenghuodi()
    import config as cfg
    from fhd_client import verify_callback_sign
    form = dict(await request.form())
    # 也兼容 JSON
    if not form:
        try:
            form = await request.json()
        except Exception:
            form = {}
    psecret = getattr(cfg, 'FENGHUODI_PSECRET', '') or ''
    if psecret and form.get('sign') and not verify_callback_sign(psecret, form):
        return JSONResponse({'success': False, 'errorCode': '1000', 'errorMsg': '签名错误'}, status_code=400)

    import json as _json
    params_raw = form.get('params') or '{}'
    try:
        params = _json.loads(params_raw) if isinstance(params_raw, str) else (params_raw or {})
    except Exception:
        params = {}

    trade_id = str(params.get('tradeId') or params.get('orderNo') or '')
    order_id_list = params.get('orderIdList') or []
    waybills = []
    for key in ('waybillCode', 'wlbCode', 'waybillCodeList'):
        for code in _waybill_values(params.get(key)):
            if code not in waybills:
                waybills.append(code)
    waybill = waybills[0] if waybills else ''
    cp_code = str(params.get('cpCode') or '')
    cp_name = str(params.get('cpName') or '')

    # 尝试匹配本地订单并标记发货
    session = next(db.get_db())
    try:
        order = _find_order_for_fhd_callback(session, trade_id, order_id_list)
        if order and order.status != 'cancelled':
            old_status = order.status
            old_waybills = set(_fhd_waybill_codes(order, session))
            from datetime import datetime as dt
            order.status = 'shipped'
            if not getattr(order, 'shipped_at', None):
                order.shipped_at = dt.utcnow()
            if waybill:
                order.fhd_waybill_code = waybill
            if cp_code:
                order.fhd_cp_code = cp_code
            if cp_name:
                order.fhd_cp_name = cp_name
            order.fhd_push_status = 'success'
            order.fhd_trade_id = trade_id or getattr(order, 'fhd_trade_id', None)
            order.fhd_last_push_at = dt.utcnow()
            _upsert_fhd_callback_logs(
                session, order, _fhd_trade_id_candidates(trade_id, order_id_list), waybills,
                cp_code=cp_code, cp_name=cp_name,
            )
            # 备注追加运单
            note = (order.notes or '')
            extra = f"[FHD] {cp_name or cp_code} {waybill}".strip()
            if extra not in note:
                order.notes = (note + ' ' + extra).strip()
            session.commit()
            # 可选通知用户
            try:
                if order.user and order.user.telegram_id and (
                    old_status != 'shipped' or not set(waybills).issubset(old_waybills)
                ):
                    from account_notifications import order_status_notice
                    await order_status_notice(db, session, order, 'shipped',
                        build_shipping_notify_text(order, session), send_telegram_message_async)
            except Exception:
                pass
        return {'success': True, 'errorCode': '', 'errorMsg': ''}
    except Exception as e:
        session.rollback()
        return JSONResponse({'success': False, 'errorCode': '1003', 'errorMsg': str(e)}, status_code=500)
    finally:
        session.close()


@app.post("/api/fenghuodi/callback/notify")
async def fhd_notify_callback(request: Request):
    """打印/发货通知接收（订单通知对接文档）。

    官方通知：header 带 pid/timestamp/nonceStr/sign，body 为 JSON。
    type:
      - tradePrint  交易打印
      - tradeSend   交易发货
      - sendOrder   订单发货
    返回：{"code":0,"message":"ok"}
    """
    _raise_retired_fenghuodi()
    import json as _json
    import config as cfg
    from datetime import datetime as dt
    from fhd_client import verify_notify_sign

    headers = {k.lower(): v for k, v in request.headers.items()}
    raw_body = await request.body()
    body_text = raw_body.decode('utf-8', errors='ignore') if raw_body else ''
    body = {}
    if body_text:
        try:
            body = json.loads(body_text)
        except Exception:
            body = {}
    if not body:
        try:
            form = dict(await request.form())
            if form:
                body = form
        except Exception:
            body = {}

    # 记录原始通知
    try:
        from pathlib import Path as _P
        log = _P('logs')
        log.mkdir(exist_ok=True)
        with open(log / 'fhd_notify.log', 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'headers': {
                    'pid': headers.get('pid'),
                    'timestamp': headers.get('timestamp'),
                    'nonceStr': headers.get('noncestr') or headers.get('nonce-str'),
                    'sign': headers.get('sign'),
                },
                'body': body,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    psecret = getattr(cfg, 'FENGHUODI_PSECRET', '') or ''
    if psecret and headers.get('sign'):
        ok = verify_notify_sign(
            psecret,
            pid=headers.get('pid', ''),
            timestamp=headers.get('timestamp', ''),
            nonce_str=headers.get('noncestr') or headers.get('nonceStr') or '',
            body_json=body_text if body_text else json.dumps(body, ensure_ascii=False, separators=(',', ':')),
            sign=headers.get('sign', ''),
        )
        if not ok:
            return JSONResponse({'code': 1000, 'message': '签名错误'}, status_code=400)

    ntype = str(body.get('type') or '')
    message = body.get('message') or {}
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except Exception:
            message = {'raw': message}
    if not isinstance(message, dict):
        message = {}

    trade_id = str(
        message.get('tradeId')
        or message.get('orderNo')
        or body.get('orderNo')
        or body.get('tradeId')
        or ''
    )
    order_id_list = message.get('orderIdList') or body.get('orderIdList') or []
    waybills = []
    for key in ('waybillCode', 'wlbCode', 'waybillCodeList', 'wlbCodes'):
        for code in _waybill_values(message.get(key)):
            if code not in waybills:
                waybills.append(code)
    # sendOrder carries its fields under message but may use the top-level
    # orderNo/type envelope; accept top-level waybill fields as well.
    for key in ('waybillCode', 'wlbCode', 'waybillCodeList', 'wlbCodes'):
        for code in _waybill_values(body.get(key)):
            if code not in waybills:
                waybills.append(code)
    waybill = waybills[0] if waybills else ''
    cp_code = str(message.get('cpCode') or body.get('cpCode') or '')
    cp_name = str(message.get('cpName') or body.get('cpName') or '')

    # 打印/发货通知同步本地订单
    if trade_id and ntype in ('tradePrint', 'tradeSend', 'sendOrder'):
        session = next(db.get_db())
        try:
            order = _find_order_for_fhd_callback(session, trade_id, order_id_list)
            if order and order.status != 'cancelled':
                old_status = order.status
                old_waybills = set(_fhd_waybill_codes(order, session))
                note = (order.notes or '')
                if ntype == 'tradePrint':
                    extra = f"[FHD-PRINT] {cp_name or cp_code} {waybill}".strip()
                    if extra not in note:
                        order.notes = (note + ' ' + extra).strip()
                else:
                    order.status = 'shipped'
                    if not getattr(order, 'shipped_at', None):
                        order.shipped_at = dt.utcnow()
                    if waybill:
                        order.fhd_waybill_code = waybill
                    if cp_code:
                        order.fhd_cp_code = cp_code
                    if cp_name:
                        order.fhd_cp_name = cp_name
                    order.fhd_push_status = 'success'
                    order.fhd_trade_id = trade_id or getattr(order, 'fhd_trade_id', None)
                    order.fhd_last_push_at = dt.utcnow()
                    _upsert_fhd_callback_logs(
                        session, order, _fhd_trade_id_candidates(trade_id, order_id_list), waybills,
                        cp_code=cp_code, cp_name=cp_name,
                    )
                    extra = f"[FHD] {cp_name or cp_code} {waybill}".strip()
                    if extra not in note:
                        order.notes = (note + ' ' + extra).strip()
                    session.commit()
                    try:
                        if order.user and order.user.telegram_id and (
                            old_status != 'shipped' or not set(waybills).issubset(old_waybills)
                        ):
                            from account_notifications import order_status_notice
                            await order_status_notice(db, session, order, 'shipped',
                                build_shipping_notify_text(order, session), send_telegram_message_async)
                    except Exception:
                        pass
                session.commit()
        except Exception as e:
            session.rollback()
            return JSONResponse({'code': 1003, 'message': str(e)}, status_code=500)
        finally:
            session.close()

    return {'code': 0, 'message': 'ok'}







@app.get("/admin/blocked_addresses", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_blocked_addresses_page(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    return templates.TemplateResponse(request, "admin/blocked_addresses.html", {})


@app.get("/api/blocked_addresses")
@_run_blocking_endpoint
def api_list_blocked_addresses(
    keyword: Optional[str] = None,
    active_only: bool = False,
    _auth: str = Depends(require_admin_or_api_secret),
):
    return db.list_blocked_addresses(active_only=active_only, keyword=keyword)


@app.post("/api/blocked_addresses")
@_run_blocking_endpoint
def api_add_blocked_address(
    address_text: str = Form(...),
    reason: Optional[str] = Form(None),
    match_mode: str = Form("contains"),
    _auth: str = Depends(require_admin_or_api_secret),
):
    result = db.add_blocked_address(address_text=address_text, reason=reason, match_mode=match_mode)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "添加失败")
    return result


@app.post("/api/blocked_addresses/bulk")
@_run_blocking_endpoint
def api_bulk_add_blocked_addresses(
    content: str = Form(...),
    reason: Optional[str] = Form("诈骗地址"),
    match_mode: str = Form("contains"),
    _auth: str = Depends(require_admin_or_api_secret),
):
    """批量上传异常地址，每行一个，也支持逗号分隔。"""
    return db.bulk_add_blocked_addresses(content, reason=reason, match_mode=match_mode)


@app.post("/api/blocked_addresses/{item_id}/delete")
@_run_blocking_endpoint
def api_delete_blocked_address(
    item_id: int,
    hard: bool = Form(False),
    _auth: str = Depends(require_admin_or_api_secret),
):
    result = db.delete_blocked_address(item_id, hard=bool(hard))
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "删除失败")
    return result


@app.post('/api/blocked_addresses/{item_id}/update')
@_run_blocking_endpoint
def api_update_blocked_address(
    item_id: int, address_text: str = Form(...), reason: str = Form(...),
    match_mode: str = Form(...), is_active: bool = Form(True),
    _auth: str = Depends(require_admin_or_api_secret),
):
    result = db.update_blocked_address(item_id, address_text, reason, match_mode, is_active)
    if not result.get('success'):
        raise HTTPException(status_code=400, detail=result.get('error') or '更新失败')
    return result


@app.post("/api/blocked_addresses/check")
@_run_blocking_endpoint
def api_check_blocked_address(
    address_text: str = Form(...),
    _auth: str = Depends(require_admin_or_api_secret),
):
    blocked, hit = db.is_address_blocked(address_text)
    return {
        "blocked": bool(blocked),
        "matched": None if not hit else {
            "id": hit.id,
            "address_text": hit.address_text,
            "reason": hit.reason,
            "match_mode": hit.match_mode,
        }
    }


@app.get("/admin/settings", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_settings_page(request: Request):
    redir = require_admin_session(request)
    if redir is not None:
        return redir
    import secrets
    request.session.setdefault('shipping_csrf',secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "admin/settings.html", {})


@app.get("/admin/fenghuodi/logs", response_class=HTMLResponse)
@_run_blocking_endpoint
def admin_fenghuodi_logs_page(request: Request):
    raise HTTPException(status_code=404, detail="页面已移除")


def ensure_business_schema():
    """为已有 SQLite 库补充业务字段/表（兼容旧库）"""
    from sqlalchemy import text
    engine = db.engine
    with engine.connect() as conn:
        def cols(table):
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            return {r[1] for r in rows}

        def drop_unique_product_name_index():
            try:
                indexes = conn.execute(text("PRAGMA index_list(products)")).fetchall()
                for idx in indexes:
                    index_name = idx[1]
                    is_unique = bool(idx[2])
                    if not is_unique or str(index_name).startswith("sqlite_autoindex"):
                        continue
                    info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
                    index_cols = [r[2] for r in info]
                    if index_cols == ["name"]:
                        conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
                        print("dropped unique product name index:", index_name)
            except Exception as e:
                print("drop unique product name index skip:", e)

        def rebuild_products_without_name_unique():
            try:
                indexes = conn.execute(text("PRAGMA index_list(products)")).fetchall()
                has_unique_name = False
                for idx in indexes:
                    index_name = idx[1]
                    is_unique = bool(idx[2])
                    if not is_unique:
                        continue
                    info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
                    if [r[2] for r in info] == ["name"]:
                        has_unique_name = True
                        break
                if not has_unique_name:
                    return
                cols_now = cols("products")
                if "low_stock_threshold" not in cols_now:
                    conn.execute(text("ALTER TABLE products ADD COLUMN low_stock_threshold INTEGER DEFAULT 20"))
                    cols_now = cols("products")
                if "is_archived" not in cols_now:
                    conn.execute(text("ALTER TABLE products ADD COLUMN is_archived BOOLEAN DEFAULT 0 NOT NULL"))
                    cols_now = cols("products")
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("DROP TABLE IF EXISTS products_migrate_new"))
                conn.execute(text("""
                    CREATE TABLE products_migrate_new (
                        id INTEGER NOT NULL,
                        name VARCHAR(100) NOT NULL,
                        cost_price FLOAT DEFAULT 0.0 NOT NULL,
                        level1_agent_price FLOAT DEFAULT 0.0 NOT NULL,
                        level2_agent_price FLOAT DEFAULT 0.0 NOT NULL,
                        regular_price FLOAT NOT NULL,
                        description TEXT,
                        default_warehouse_id INTEGER,
                        parent_id INTEGER,
                        is_parent BOOLEAN,
                        low_stock_threshold INTEGER DEFAULT 20 NOT NULL,
                        category_serial INTEGER,
                        product_serial INTEGER,
                        auto_unlisted BOOLEAN DEFAULT 0 NOT NULL,
                        is_archived BOOLEAN DEFAULT 0 NOT NULL,
                        is_active BOOLEAN,
                        created_at DATETIME,
                        PRIMARY KEY (id),
                        FOREIGN KEY(default_warehouse_id) REFERENCES warehouses (id),
                        FOREIGN KEY(parent_id) REFERENCES products (id)
                    )
                """))
                copy_cols = [
                    "id", "name", "cost_price", "level1_agent_price", "level2_agent_price",
                    "regular_price", "description", "default_warehouse_id", "parent_id",
                    "is_parent", "low_stock_threshold", "category_serial", "product_serial",
                    "auto_unlisted", "is_archived", "is_active", "created_at"
                ]
                existing_cols = [c for c in copy_cols if c in cols_now]
                conn.execute(text(f"""
                    INSERT INTO products_migrate_new ({','.join(existing_cols)})
                    SELECT {','.join(existing_cols)} FROM products
                """))
                conn.execute(text("DROP TABLE products"))
                conn.execute(text("ALTER TABLE products_migrate_new RENAME TO products"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_id ON products (id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_name ON products (name)"))
                conn.execute(text("PRAGMA foreign_keys=ON"))
                print("rebuilt products table without global unique name")
            except Exception as e:
                print("rebuild products unique name skip:", e)
                try:
                    conn.execute(text("PRAGMA foreign_keys=ON"))
                except Exception:
                    pass

        # users new columns
        try:
            user_cols = cols('users')
            alter_users = []
            if 'telegram_name' not in user_cols:
                alter_users.append("ALTER TABLE users ADD COLUMN telegram_name VARCHAR(150)")
            if 'display_name' not in user_cols:
                alter_users.append("ALTER TABLE users ADD COLUMN display_name VARCHAR(100)")
            if 'preferred_pay_method' not in user_cols:
                alter_users.append("ALTER TABLE users ADD COLUMN preferred_pay_method VARCHAR(20) DEFAULT 'cny'")
            if 'remark' not in user_cols:
                alter_users.append("ALTER TABLE users ADD COLUMN remark TEXT")
            for sql in alter_users:
                conn.execute(text(sql))
        except Exception as e:
            print('migrate users skip:', e)

        # products new columns / old unique name index
        try:
            product_cols = cols('products')
            alter_products = []
            if 'low_stock_threshold' not in product_cols:
                alter_products.append("ALTER TABLE products ADD COLUMN low_stock_threshold INTEGER DEFAULT 20")
            if 'category_serial' not in product_cols:
                alter_products.append("ALTER TABLE products ADD COLUMN category_serial INTEGER")
            if 'product_serial' not in product_cols:
                alter_products.append("ALTER TABLE products ADD COLUMN product_serial INTEGER")
            if 'auto_unlisted' not in product_cols:
                alter_products.append("ALTER TABLE products ADD COLUMN auto_unlisted BOOLEAN DEFAULT 0 NOT NULL")
            if 'is_archived' not in product_cols:
                alter_products.append("ALTER TABLE products ADD COLUMN is_archived BOOLEAN DEFAULT 0 NOT NULL")
            for sql in alter_products:
                conn.execute(text(sql))
            drop_unique_product_name_index()
            rebuild_products_without_name_unique()
            try:
                conn.execute(text("UPDATE products SET auto_unlisted = 0 WHERE auto_unlisted IS NULL"))
                conn.execute(text("UPDATE products SET is_archived = 0 WHERE is_archived IS NULL"))
                conn.execute(text("UPDATE products SET is_active = 1 WHERE COALESCE(auto_unlisted, 0) = 1"))
                max_category = conn.execute(text("SELECT COALESCE(MAX(category_serial), 0) FROM products WHERE COALESCE(is_parent, 0) = 1")).scalar() or 0
                for row in conn.execute(text("SELECT id FROM products WHERE COALESCE(is_parent, 0) = 1 AND (category_serial IS NULL OR category_serial <= 0) ORDER BY id")).fetchall():
                    max_category += 1
                    conn.execute(text("UPDATE products SET category_serial = :serial WHERE id = :id"), {"serial": max_category, "id": row[0]})
                max_product = conn.execute(text("SELECT COALESCE(MAX(product_serial), 0) FROM products WHERE COALESCE(is_parent, 0) = 0")).scalar() or 0
                for row in conn.execute(text("SELECT id FROM products WHERE COALESCE(is_parent, 0) = 0 AND (product_serial IS NULL OR product_serial <= 0) ORDER BY id")).fetchall():
                    max_product += 1
                    conn.execute(text("UPDATE products SET product_serial = :serial WHERE id = :id"), {"serial": max_product, "id": row[0]})
                for row in conn.execute(text("""
                    SELECT id, COALESCE(low_stock_threshold, 20), COALESCE(auto_unlisted, 0)
                    FROM products
                    WHERE COALESCE(is_parent, 0) = 0
                    ORDER BY id
                """)).fetchall():
                    product_id, threshold, auto_unlisted = int(row[0]), normalize_low_stock_threshold(row[1]), bool(row[2])
                    total_stock = conn.execute(text("""
                        SELECT COALESCE(SUM(quantity), 0)
                        FROM warehouse_stock
                        WHERE product_id = :product_id
                    """), {"product_id": product_id}).scalar() or 0
                    if should_auto_unlist(int(total_stock), threshold):
                        conn.execute(text("UPDATE products SET auto_unlisted = 1 WHERE id = :id"), {"id": product_id})
                    elif auto_unlisted:
                        conn.execute(text("UPDATE products SET auto_unlisted = 0 WHERE id = :id"), {"id": product_id})
            except Exception as e:
                print('assign product serials skip:', e)
        except Exception as e:
            print('migrate products skip:', e)

        # orders new columns
        try:
            order_cols = cols('orders')
            alter_orders = []
            if 'paid_amount' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN paid_amount FLOAT DEFAULT 0")
            if 'payment_status' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN payment_status VARCHAR(20) DEFAULT 'unpaid'")
            if 'shipped_at' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN shipped_at DATETIME")
            if 'fhd_push_status' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_push_status VARCHAR(20) DEFAULT 'not_pushed'")
            if 'fhd_push_mode' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_push_mode VARCHAR(50)")
            if 'fhd_push_count' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_push_count INTEGER DEFAULT 0")
            if 'fhd_last_push_at' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_last_push_at DATETIME")
            if 'fhd_last_error' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_last_error TEXT")
            if 'fhd_trade_id' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_trade_id VARCHAR(80)")
            if 'fhd_waybill_code' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_waybill_code VARCHAR(100)")
            if 'fhd_cp_code' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_cp_code VARCHAR(50)")
            if 'fhd_cp_name' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN fhd_cp_name VARCHAR(100)")
            if 'source_chat_id' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN source_chat_id VARCHAR(64)")
            if 'source_message_id' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN source_message_id INTEGER")
            if 'source_chat_type' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN source_chat_type VARCHAR(20)")
            if 'source_chat_title' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN source_chat_title VARCHAR(255)")
            if 'split_group_id' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN split_group_id VARCHAR(80)")
            if 'split_index' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN split_index INTEGER")
            if 'split_count' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN split_count INTEGER DEFAULT 1 NOT NULL")
            if 'split_confirmed_at' not in order_cols:
                alter_orders.append("ALTER TABLE orders ADD COLUMN split_confirmed_at DATETIME")
            for sql in alter_orders:
                conn.execute(text(sql))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_split_group_id ON orders (split_group_id)"))
        except Exception as e:
            print('migrate orders skip:', e)

        # payment exchange-rate audit fields
        try:
            payment_cols = cols('payments')
            alter_payments = []
            if 'original_amount' not in payment_cols:
                alter_payments.append("ALTER TABLE payments ADD COLUMN original_amount FLOAT")
            if 'exchange_rate' not in payment_cols:
                alter_payments.append("ALTER TABLE payments ADD COLUMN exchange_rate FLOAT")
            if 'exchange_currency' not in payment_cols:
                alter_payments.append("ALTER TABLE payments ADD COLUMN exchange_currency VARCHAR(20)")
            for sql in alter_payments:
                conn.execute(text(sql))
        except Exception as e:
            print('migrate payments skip:', e)

        # order_items cost columns
        try:
            oi_cols = cols('order_items')
            alter_oi = []
            if 'unit_cost' not in oi_cols:
                alter_oi.append("ALTER TABLE order_items ADD COLUMN unit_cost FLOAT DEFAULT 0")
            if 'cost_amount' not in oi_cols:
                alter_oi.append("ALTER TABLE order_items ADD COLUMN cost_amount FLOAT DEFAULT 0")
            for sql in alter_oi:
                conn.execute(text(sql))
        except Exception as e:
            print('migrate order_items skip:', e)

        # stock_movements batch columns
        try:
            mv_cols = cols('stock_movements')
            alter_mv = []
            if 'unit_cost' not in mv_cols:
                alter_mv.append("ALTER TABLE stock_movements ADD COLUMN unit_cost FLOAT")
            if 'batch_no' not in mv_cols:
                alter_mv.append("ALTER TABLE stock_movements ADD COLUMN batch_no VARCHAR(50)")
            if 'remain_qty' not in mv_cols:
                alter_mv.append("ALTER TABLE stock_movements ADD COLUMN remain_qty INTEGER")
            for sql in alter_mv:
                conn.execute(text(sql))
        except Exception as e:
            print('migrate stock_movements skip:', e)

        conn.commit()
    # create new tables
    from models import Base
    Base.metadata.create_all(bind=db.engine)
    print('business schema ensured')


def init_data():
    """初始化数据"""
    try:
        _load_runtime_settings()
    except Exception as _e:
        print("load settings skip:", _e)
    print("初始化数据...")
    ensure_business_schema()
    session = next(db.get_db())

    # 创建默认仓库（如果没有）
    warehouse = session.query(Warehouse).first()
    if not warehouse:
        print("创建默认仓库...")
        warehouse = Warehouse(name="默认仓库", address="默认地址", shipping_fee=0.0)
        session.add(warehouse)
        session.flush()
        print("已创建默认仓库")

    # 检查是否已有商品
    existing_products = session.query(Product).count()
    if existing_products == 0:
        print("创建默认商品...")
        default_parent = Product(
            name="默认分类",
            is_parent=True,
            regular_price=0,
            low_stock_threshold=20,
            category_serial=1,
        )
        session.add(default_parent)
        session.flush()
        for product_index, (name, price) in enumerate(PRODUCTS.items(), start=1):
            initial_quantity = INITIAL_STOCK.get(name, 0)
            product = Product(
                name=name,
                cost_price=round(price * 0.6, 2),
                level1_agent_price=round(price * 0.8, 2),
                level2_agent_price=round(price * 0.9, 2),
                regular_price=price,
                description=f"{name} - 单价¥{price}",
                default_warehouse_id=warehouse.id,
                parent_id=default_parent.id,
                low_stock_threshold=20,
                product_serial=product_index,
            )
            session.add(product)
            session.flush()

            # 在默认仓库中创建库存
            warehouse_stock = WarehouseStock(
                warehouse_id=warehouse.id,
                product_id=product.id,
                quantity=initial_quantity
            )
            session.add(warehouse_stock)
            db.refresh_product_stock_status(session, product.id)

        session.commit()
        print(f"创建了 {len(PRODUCTS)} 个商品")
    else:
        print("商品已存在，跳过初始化")

    print("数据初始化完成！")


async def _inventory_admin_rows(warehouse_id):
    from inventory_preview import catalog_inventory_rows
    return catalog_inventory_rows(db,warehouse_id)


@app.get('/api/inventory-image/catalog')
@_run_blocking_endpoint
def inventory_image_catalog(_auth: str = Depends(require_admin_or_api_secret)):
    from inventory_preview import catalog_inventory_rows
    return catalog_inventory_rows(db)


from inventory_admin import make_router as make_inventory_router
from inventory_operations_admin import make_router as make_inventory_operations_router
app.include_router(make_inventory_operations_router(db,
    lambda: ordinary_business_ready(SESSION_SECRET_KEY), admin_username=lambda: ADMIN_USERNAME))
app.include_router(make_inventory_router(templates, _inventory_admin_rows,
    lambda: ordinary_business_ready(SESSION_SECRET_KEY), admin_username=lambda: ADMIN_USERNAME,
    warehouse_provider=lambda: _image_warehouses()))

def _image_warehouses():
    with db.SessionLocal() as session:
        from catalog_retirement import operating_query
        return [dict(id=w.id,name=w.name) for w in operating_query(session, Warehouse, 'warehouse').all()]


if __name__ == "__main__":
    # 初始化数据
    init_data()

    # 启动服务器
    print(f"=" * 50)
    print(f"API服务启动: http://{API_HOST}:{API_PORT}")
    print(f"管理后台: http://{API_HOST}:{API_PORT}/admin")
    print(f"=" * 50)

    uvicorn.run(
        "api:app",
        host=API_HOST,
        port=API_PORT,
        reload=True,
        proxy_headers=False,
        log_level="info"
    )
