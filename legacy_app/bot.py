import logging
import time
from execution_safety import ExecutionStopped
import asyncio
import unicodedata
from telegram_delivery import TelegramDeliveryLimiter, TelegramError, inventory_document_transaction
from telegram_delivery_state import DeliveryState
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from telegram import (
    Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, MenuButtonCommands
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)
from config import (
    TELEGRAM_BOT_TOKEN, COMMANDS, ORDER_FORMAT_EXAMPLE,
    WEBHOOK_URL, WEBHOOK_PORT, API_BASE_URL,
    ADMIN_TELEGRAM_IDS, INBOUND_FORMAT_EXAMPLE, ORDER_GUIDE,
    STOCK_AUTO_UNLIST_MODE, BOT_PROFILE, IS_WHOLESALE_BOT, AGENT_GROUP_IDS
)
from config import DEEPSEEK_ENABLED, DEEPSEEK_MIN_CONFIDENCE, AFTERSALE_NOTIFY_IDS
from order_parser import OrderParser
from contact_fields import parse_contact, merge_contact, diagnose_contact, contact_prompt, name_has_evidence, FIELDS, record_contact_evidence
from ai_settings import get_settings as get_ai_settings, AIConfigError, mark_validation_failure
from ai_diagnostics import request as diagnostic_request, event as diagnostic_event, reject as diagnostic_reject
from deepseek_client import parse_contact_ai
from api_client import api_client
# 停仓规则（按仓库）。放顶层是刻意的：漏部署要立刻起不来，
# 而不是等到客户下单那一刻才 ImportError。
import sales_pause
# 数量/包裹限额（快递对单地址每天件数有上限，超了会集包扣件）。同样顶层 import。
import package_rules
from recognition_work import run as recognition_work
from recognition_work import run_light as light_recognition_work
from deepseek_client import parse_order as parse_order_with_deepseek, to_order_info
from deepseek_resolver import (
    enforce_generation as enforce_category_generation,
    resolve_category as resolve_catalog_category,
    resolve_item as resolve_ai_item,
)
import fulfillment
from fulfillment import from_ai_result as fulfillment_from_ai
from inventory_preview import (
    filter_visible_stock_rows,
    inventory_image_settings,
    render_inventory_image,
    render_inventory_image_bytes,
    snapshots_from_stock_rows,
)
import tempfile
import os
import json
import re
import requests
import html

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Avoid leaking Telegram Bot API URLs (which contain the Bot Token) through
# httpx/httpcore INFO request logs. Keep application logs at INFO.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _record_learning_usage_noncritical(*, hits, ai_called):
    """Record an optional counter without delaying or failing an order."""
    if not hits:
        return
    try:
        from learning_usage import try_record
        await asyncio.wait_for(
            asyncio.to_thread(try_record,hits=hits,ai_called=ai_called),
            timeout=0.25,
        )
    except Exception as error:
        logger.info('Learning usage metric skipped type=%s',type(error).__name__)


CHINA_TZ = timezone(timedelta(hours=8))


def clean_telegram_text(value) -> str:
    """Replace unpaired UTF-16 surrogates rejected by Telegram's JSON encoder."""
    text = str(value or '')
    return ''.join('\ufffd' if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def build_unit_price_rows(products, price_map):
    """Collapse flavor rows into one user-facing price row per category."""
    groups = {}
    for product in products or []:
        category = str(product.get('parent_name') or product.get('name') or '').strip()
        if not category:
            continue
        product_id = product.get('id')
        info = (price_map or {}).get(product_id)
        if info is None:
            info = (price_map or {}).get(str(product_id))
        raw_price = info.get('unit_price') if info else product.get('price', product.get('regular_price', 0))
        try:
            price = round(float(raw_price or 0), 2)
        except (TypeError, ValueError):
            continue
        groups.setdefault(category, set()).add(price)

    rows = []
    for category in sorted(groups):
        prices = sorted(groups[category])
        price_text = (
            f"¥{prices[0]:.2f}/个" if len(prices) == 1
            else f"¥{prices[0]:.2f}-¥{prices[-1]:.2f}/个"
        )
        rows.append((category, price_text))
    return rows


def build_warehouse_price_sections(products, warehouses, price_map):
    """Presentation follows physical catalog relations, not price history."""
    sections=[]
    for warehouse in warehouses or []:
        if not warehouse.get('is_active', True):
            continue
        rows=build_unit_price_rows([
            p for p in products or []
            if str(p.get('default_warehouse_id')) == str(warehouse['id'])
            and p.get('parent_id') and p.get('parent_name')
            and p.get('is_active', True) and not p.get('is_archived', False)
        ],price_map)
        if rows:
            sections.append((str(warehouse.get('name') or ''),rows))
    return sections


def format_beijing_time(value) -> str:
    """Format stored UTC timestamps as Beijing time at the Telegram boundary."""
    if not value:
        return "-"
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def normalize_threshold(value, default: int = 20) -> int:
    if value is None:
        return max(0, int(default))
    if isinstance(value, str) and value.strip() == "":
        return max(0, int(default))
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return max(0, int(default))


def stock_pause_mode() -> str:
    mode = (STOCK_AUTO_UNLIST_MODE or "threshold").strip().lower()
    return "zero" if mode in ("zero", "stock_zero", "order") else "threshold"


def is_stock_paused(stock: int, threshold: int) -> bool:
    stock = max(0, int(stock or 0))
    threshold = normalize_threshold(threshold)
    if stock_pause_mode() == "zero":
        return stock <= 0
    return stock <= threshold


def stock_pause_text(stock: int, threshold: int) -> str:
    if stock_pause_mode() == "zero":
        return f"该商品库存为0，暂不出货（当前 {stock}）"
    return f"该商品已到达预警库存，暂不出货（当前 {stock}，预警 {threshold}）"


class OrderBot:
    """Telegram自动下单机器人"""

    def __init__(self):
        self.app = None
        self.use_webhook = bool(WEBHOOK_URL)
        self._queue_runtime = None

    async def _start_durable_queue(self, application):
        import os
        if os.getenv('NEWBOT_INGESTION_POLICY_PATH')=='/etc/newbot/ingestion-policy.json':
            from success_receipt_audit import assert_quiet
            await asyncio.to_thread(assert_quiet)
        from models import Database
        from config import (SESSION_SECRET_KEY, ORDER_MESSAGE_WORKERS,
                            ORDER_CALLBACK_WORKERS)
        from telegram_order_queue import Queue, migrate
        from telegram_queue_runtime import Runtime
        database = await asyncio.to_thread(Database)
        def initialize():
            with database.engine.begin() as connection:
                migrate(connection)
        await asyncio.to_thread(initialize)
        self._queue_runtime = Runtime(
            self, Queue(database, SESSION_SECRET_KEY), application,
            workers=ORDER_MESSAGE_WORKERS,
            callback_workers=ORDER_CALLBACK_WORKERS,
        )
        logger.info('ORDER_QUEUE_READY message_workers=%d callback_workers=%d chat_limit=%d local_cpu_workers=2 delivery_fifo=enabled waiting_notices=disabled',
                    ORDER_MESSAGE_WORKERS,ORDER_CALLBACK_WORKERS,self._queue_runtime.queue.message_chat_limit)
        await self._setup_bot_menu(application)

    async def handle_callback_ingress(self,update,context):
        # IngressQueue has acknowledged and persisted the update before the
        # polling offset advances. Business effects run only in leased workers.
        if self._queue_runtime is None:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text('按钮处理暂时不可用，请稍后重试；本次未执行。')

    async def _stop_durable_queue(self, application):
        if self._queue_runtime:
            await self._queue_runtime.close()

    async def receive_order_message(self, update, context):
        if self._queue_runtime is None:
            raise RuntimeError('ORDER_QUEUE_NOT_INITIALIZED')
        await self._queue_runtime.receive(update, context)

    async def _api_call(self, method_name: str, *args, **kwargs):
        """Run the synchronous APIClient in a worker thread.

        Telegram handlers share one asyncio loop; a requests timeout in the
        legacy client must not pause polling or callback processing.
        """
        started = asyncio.get_running_loop().time()
        try:
            return await asyncio.to_thread(getattr(api_client, method_name), *args, **kwargs)
        finally:
            elapsed = asyncio.get_running_loop().time() - started
            from order_trace import mark
            mark({'get_telegram_account':'PERMISSION','create_order':'CREATE_ORDER',
                  'get_products':'CATALOG','get_all_warehouses':'WAREHOUSES',
                  'get_warehouse_stock':'STOCK'}.get(method_name,'API_'+method_name),elapsed*1000)
            if elapsed >= 1.0:
                logger.warning('APIClient call slow method=%s elapsed=%.3fs', method_name, elapsed)

    @__import__('order_trace').timed_async('VISIBLE_CATALOG')
    async def _visible_catalog_for_user_async(self, update: Update):
        telegram_user = getattr(update, 'effective_user', None)
        user = None
        if telegram_user:
            _ok, user = await self._api_call(
                'create_or_get_user',
                str(telegram_user.id),
                getattr(telegram_user, 'username', None),
                getattr(telegram_user, 'full_name', None),
            )
        # Fresh read-only requests may overlap. No cross-order cache, no
        # permission/pricing snapshot reuse and no parallel business writes.
        warehouse_rows,product_rows=await asyncio.gather(
            self._api_call('get_all_warehouses'),self._api_call('get_products'))
        warehouses = self._visible_warehouses(warehouse_rows, update=update, user=user)
        products = self._visible_products(
            product_rows, warehouses,
        )
        return products, warehouses, user

    async def _product_stock_map_async(self, warehouses: Optional[List[Dict]] = None, products: Optional[List[Dict]] = None) -> Dict[int, int]:
        # This is a local Bot aggregation helper, not an APIClient method.
        return await asyncio.to_thread(self._product_stock_map, warehouses, products)

    async def _send_order_confirmation(self, bot: Bot, chat_id: int, order_info: Dict):
        """发送订单确认信息（包含仓库匹配结果与剩余库存）"""
        from success_receipt_audit import ReceiptSender
        bot=ReceiptSender(bot,order_info)
        if order_info.get('_receipt_message'):
            order_info=dict(order_info,_receipt_message=ReceiptSender(order_info['_receipt_message'],order_info))
        # Keep immutable internal references in delivery audit; display exactly
        # the same public number used by the admin order list.
        order_info=dict(order_info)
        order_info['order_number']=order_info.get('display_order_number') or order_info.get('order_number')
        order_info['order_numbers']=order_info.get('display_order_numbers') or order_info.get('order_numbers')
        if order_info.get('_group_receipt') or order_info.get('_automatic_receipt'):
            from order_trace import bind_receipt_source,receipt_plan,mark
            runtime=getattr(self,'_queue_runtime',None)
            database=runtime.queue.database if runtime is not None else None
            await asyncio.to_thread(bind_receipt_source,database,chat_id,order_info.get('source_message_id'))
            generate_started=time.perf_counter()
            rows=['✅ 录单成功','订单号：'+ '、'.join(order_info.get('order_numbers') or [str(order_info.get('order_number') or '')]),
                html.escape(str(order_info.get('recipient_name') or ''))+' '+html.escape(str(order_info.get('recipient_phone') or '')),
                html.escape(str(order_info.get('recipient_address') or '')),'']
            if order_info.get('_recipient_generated'):
                rows.append('⚠️ 原文缺少收件人姓名，已使用管理员配置的临时收件名称。')
            groups={}
            bundle_sales=order_info.get('bundle_sales') or []
            bundled_item_ids={id for sale in bundle_sales for id in sale['item_ids']}
            for sale in bundle_sales:
                rows.append(html.escape(sale['name'])+'×'+str(sale['quantity']))
                if sale.get('show_flavors'):
                    counts={}
                    for item in sale['items']:
                        key=(item['product_name'],__import__('product_units').item_unit(item))
                        counts[key]=counts.get(key,0)+int(item['quantity'])
                    rows.append('配货：'+'、'.join(html.escape(n)+'×'+str(q)+(u if u!='支' else '') for (n,u),q in counts.items()))
            for item in order_info.get('items',[]):
                if item.get('item_id') in bundled_item_ids:continue
                key=(str(item.get('warehouse_name') or ''),str(item.get('parent_name') or ''))
                unit=__import__('product_units').item_unit(item)
                groups.setdefault(key,[]).append(html.escape(str(item.get('product_name') or ''))+'×'+str(item['quantity'])+(unit if unit!='支' else ''))
            rows.extend(html.escape(warehouse+' · '+category)+'：'+'、'.join(items) for (warehouse,category),items in groups.items())
            from order_receipt import financial_rows, receipt_chunks
            from product_units import total_label
            rows.append(total_label(order_info.get('items',[])))
            rows.extend(financial_rows(order_info))
            chunks=receipt_chunks(rows)
            mark('RECEIPT_GENERATE',(time.perf_counter()-generate_started)*1000)
            receipt_plan(len(chunks))

            receipt_message_id=order_info.get('_receipt_message_id')
            receipt_chat_id=order_info.get('_receipt_chat_id') or chat_id

            async def edit_group_receipt(chunk):
                if not receipt_message_id:
                    return None
                try:
                    await bot.edit_message_text(
                        chat_id=receipt_chat_id,
                        message_id=int(receipt_message_id),
                        text=chunk,
                        parse_mode='HTML',
                        reply_markup=None,
                    )
                    return True
                except Exception as exc:
                    from telegram.error import BadRequest, Forbidden
                    if isinstance(exc,BadRequest) and 'message is not modified' in str(exc).lower():
                        return True
                    if not isinstance(exc,(BadRequest,Forbidden)):
                        logger.warning('RECEIPT_EDIT_UNCERTAIN type=%s',type(exc).__name__)
                        return False
                    logger.warning('RECEIPT_EDIT_REJECTED type=%s',type(exc).__name__)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode='HTML',
                        reply_to_message_id=order_info.get('source_message_id'),
                    )
                    return True

            if len(chunks)>1:
                # Delivery only; never parse, create, or debit again. A network
                # failure is allowed to propagate as delivery uncertainty.
                for index,chunk in enumerate(chunks):
                    if index==0:
                        edited=await edit_group_receipt(chunk)
                        if edited is not None:
                            if not edited:return
                            continue
                    if index==0 and order_info.get('_receipt_message'):
                        await order_info['_receipt_message'].edit_text(chunk,parse_mode='HTML',reply_markup=None)
                    else:
                        await bot.send_message(chat_id=chat_id,text=chunk,parse_mode='HTML',
                            **({'reply_to_message_id':order_info['source_message_id']} if order_info.get('source_chat_type') in ('group','supergroup') else {}))
                return
            edited=await edit_group_receipt('\n'.join(rows))
            if edited is not None:
                return
            if order_info.get('_receipt_message'):
                try:
                    await order_info['_receipt_message'].edit_text('\n'.join(rows),parse_mode='HTML',reply_markup=None)
                    return
                except Exception as exc:
                    from telegram.error import BadRequest, Forbidden
                    # Ambiguous network failures must not produce a duplicate.
                    # Only a definitive edit rejection permits a new receipt.
                    if isinstance(exc,BadRequest) and 'message is not modified' in str(exc).lower():return
                    if not isinstance(exc,(BadRequest,Forbidden)):
                        logger.warning('RECEIPT_EDIT_UNCERTAIN type=%s',type(exc).__name__)
                        return
                    logger.warning('RECEIPT_EDIT_REJECTED type=%s',type(exc).__name__)
                    await bot.send_message(chat_id=chat_id,text='\n'.join(rows),parse_mode='HTML')
            else:
                await bot.send_message(chat_id=chat_id,text='\n'.join(rows),parse_mode='HTML',
                    **({'reply_to_message_id':order_info['source_message_id']} if order_info.get('source_chat_type') in ('group','supergroup') else {}))
            return
        # 适配两种数据格式
        if 'items_by_warehouse' in order_info:
            warehouse_dict = order_info['items_by_warehouse']
        else:
            warehouse_dict = {}
            for item in order_info.get('items', []):
                warehouse_name = item.get('warehouse_name', '默认仓库')
                if warehouse_name not in warehouse_dict:
                    warehouse_dict[warehouse_name] = []
                warehouse_dict[warehouse_name].append({
                    'product_name': item.get('product_name'),
                    'parent_name': item.get('parent_name') or item.get('category_name'),
                    'display_name': item.get('display_name'),
                    'quantity': item.get('quantity'),
                    'unit': __import__('product_units').item_unit(item),
                    'unit_price': item.get('unit_price', item.get('total_price', 0) / max(item.get('quantity', 1), 1)),
                    'remaining_quantity': item.get('remaining_quantity')
                })

        items_message = "\n📦 商品信息：\n" if len(warehouse_dict) <= 1 else ""
        for warehouse_name, items in warehouse_dict.items():
            if len(warehouse_dict) > 1:
                items_message += f"\n【{warehouse_name}】\n"

            for item in items:
                product_name = item.get('display_name')
                if not product_name:
                    parent_name = str(item.get('parent_name') or item.get('category_name') or '').strip()
                    product_name = (
                        f"品类({parent_name})口味({item['product_name']})"
                        if parent_name else item['product_name']
                    )
                quantity = item['quantity']
                unit_price = item.get('unit_price', 0)
                remain = item.get('remaining_quantity')
                remain_text = f" | 剩余 {remain}" if remain is not None else ""
                unit=__import__('product_units').item_unit(item)
                items_message += f"  • {product_name} x {quantity}{unit} - ¥{unit_price:0.2f}/{unit}{remain_text}\n"

            if order_info.get('total_shipping_fee') and len(warehouse_dict) > 1:
                shipping_fee = order_info['total_shipping_fee'] / len(warehouse_dict)
                items_message += f"  🚚 运费: ¥{shipping_fee:0.2f}\n"

        total_units = sum(int(item.get('quantity') or 0) for items in warehouse_dict.values() for item in items)
        from product_units import total_label
        items_message += total_label([item for items in warehouse_dict.values() for item in items])+"\n"

        remaining_lines = ""
        remaining_stock = order_info.get('remaining_stock') or []
        if remaining_stock:
            remaining_lines = "\n📉 <b>自动减库存结果：</b>\n"
            for rs in remaining_stock:
                remaining_lines += (
                    f"  • {rs.get('product_name')} @ {rs.get('warehouse_name')}: "
                    f"-{rs.get('quantity_ordered', 0)} → 剩余 {rs.get('remaining_quantity', 0)}\n"
                )

        total_message = f"""💰 金额明细：
• 商品总计: ¥{order_info.get('total_amount', 0):0.2f}
• 运费总计: ¥{order_info.get('total_shipping_fee', 0):0.2f}
━━━━━━━━━━━━━━━━
✅ 应付总额: ¥{order_info.get('grand_total', 0):0.2f}
"""

        balance_line = ""
        if order_info.get('user_balance') is not None:
            balance_line = f"\n💳 预存余额: ¥{order_info.get('user_balance'):0.2f}"

        order_numbers = [
            str(number).strip()
            for number in (order_info.get('order_numbers') or [])
            if str(number).strip()
        ]
        if not order_numbers and order_info.get('order_number'):
            order_numbers = [str(order_info.get('order_number')).strip()]
        is_split = bool(order_info.get('is_split')) or len(order_numbers) > 1
        split_count = int(order_info.get('split_count') or len(order_numbers) or 1)
        if is_split:
            order_number_message = (
                "🆔 <b>订单号：</b>\n"
                + "\n".join(f"• #{html.escape(number)}" for number in order_numbers)
                + f"\n⚠️ <b>已自动拆为 {split_count} 笔，等待后台确认</b>"
            )
        else:
            order_number = order_numbers[0] if order_numbers else 'N/A'
            order_number_message = f"🆔 <b>订单号：</b> #{html.escape(order_number)}"

        message = f"""✅ <b>订单创建成功！</b>

{items_message}
{remaining_lines}
{total_message}

🏠 <b>收货信息：</b>
• 收件人：{order_info.get('recipient_name', 'N/A')}
• 电话：{order_info.get('recipient_phone', 'N/A')}
• 地址：{order_info.get('recipient_address', 'N/A')}

{order_number_message}{balance_line}
🕒 <b>下单时间（北京时间）：</b> {format_beijing_time(order_info.get('created_at'))}

💡 <i>已生成订单并扣减库存；结账为人工收账，不强制每单一结</i>
"""

        await self._send_chunked_message(
            lambda chunk, **kwargs: bot.send_message(chat_id=chat_id, text=chunk, **kwargs),
            message,
            parse_mode='HTML'
        )

    @staticmethod
    def _agent_group_ids() -> set:
        import config as cfg
        values = getattr(cfg, 'AGENT_GROUP_IDS', AGENT_GROUP_IDS) or []
        return {int(value) for value in values if str(value).lstrip('-').isdigit()}

    @classmethod
    def _is_group_chat(cls, update: Update) -> bool:
        chat = getattr(update, 'effective_chat', None)
        chat_type = str(getattr(chat, 'type', '') or '').lower()
        return chat_type in {'group', 'supergroup'}

    @classmethod
    def _agent_group_strict(cls) -> bool:
        return str(os.getenv('AGENT_GROUP_STRICT', '')).strip().lower() in {
            '1', 'true', 'yes', 'on'
        }

    @classmethod
    def _is_agent_group(cls, update: Update) -> bool:
        """Groups carry orders; sender qualification is the only order gate."""
        return cls._is_group_chat(update)

    @staticmethod
    def _looks_like_order_candidate(text: str) -> bool:
        """Cheap high-precision gate used before AI/local order parsing."""
        value = str(text or '').strip()
        if not value:
            return False
        from order_structure import is_order_receipt
        if is_order_receipt(value):return False
        phone = re.search(r'(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)', value)
        address = any(token in value for token in (
            '地址', '省', '市', '区', '县', '镇', '乡', '街', '路', '号',
            '栋', '单元', '室', '收货', '收件',
        ))
        quantity = bool(re.search(
            r'(?:x|×|\*|数量)\s*\d+|\d+\s*(?:个|件|盒|包|瓶|罐|条|支|箱|份)|'
            r'[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z()（）\-]{0,24}\s*\d{1,5}(?!\d)',
            value,
            re.IGNORECASE,
        ))
        order_marker = any(token in value for token in (
            '下单', '订单', '商品', '品类', '口味', '数量', '收件人', '手机号',
            '手机', '电话',
        ))
        if phone and (address or quantity or order_marker):
            return True
        if address and quantity:
            return True
        return order_marker and quantity and len(value) >= 8

    @staticmethod
    def _has_pending_order_input(context: ContextTypes.DEFAULT_TYPE) -> bool:
        user_data = getattr(context, 'user_data', None) or {}
        return any(user_data.get(key) for key in (
            'wholesale_cart_quantity_input', 'wholesale_cart_checkout',
            'quick_order_item', 'pending_recipient_order',
        ))

    @staticmethod
    def _is_non_order_intent(text):
        value=str(text or '').strip()
        if value in {'吃了吗','在吗','好的','谢谢','收到','等等','今天发吗','这个还有吗',
                     '取消一下','取消订单','不要了','不用了','晚点发'}:
            return True
        return bool(re.match(r'^(?:请|麻烦)?(?:取消(?:一下|订单|刚才)|不要了|'
            r'把(?:刚才|之前|上个|这笔|这个).*订单.*(?:改|换)|'
            r'改成|换成|(?:地址|姓名|手机号|电话|仓库|数量|口味|商品|金额)(?:改成|改为|换成))',value))

    async def _reply_agent_group_message(
        self,
        update: Update,
        text: str,
        parse_mode: str = 'HTML',
        reply_markup=None,
    ) -> None:
        """Reply to the exact group message that started the order flow."""
        message = getattr(update, 'effective_message', None)
        if not message:
            return
        await self._send_chunked_message(
            message.reply_text,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            reply_to_message_id=getattr(message, 'message_id', None),
        )

    async def _start_agent_group_receipt(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Do not emit waiting acknowledgements; preserve old receipt IDs.

        Existing persisted drafts may still carry an acknowledgement message
        from v3. Final delivery may edit it, but new orders send only their
        terminal result or an indispensable customer-confirmation question.
        """
        return

    async def _ensure_agent_group_authorized(self, update: Update) -> bool:
        """Only approved dropship users may trigger group order recognition."""
        account=getattr(update,'_rainbow_account',None)
        if isinstance(account,dict):return bool(account.get('authorized'))
        if not self._is_group_chat(update):
            return True
        # Every group is eligible as a carrier; only sender approval matters.
        user = getattr(update, 'effective_user', None)
        if not user:
            return False
        try:
            status_info = await self._api_call(
                'get_dropship_auth_status', str(user.id)
            )
        except Exception as exc:
            logger.warning('群订单代发授权查询异常 user=%s: %s', user.id, exc)
            status_info = None
        if not isinstance(status_info, dict):
            return False
        if status_info.get('status') == 'approved' and status_info.get('authorized'):
            return True
        return False

    @staticmethod
    def _agent_group_order_text(preview: Dict, order: Optional[Dict] = None, error: Optional[str] = None) -> str:
        def compact_items(items):
            """按品类聚合口味，群回执与面单保持同一紧凑口径。"""
            groups = {}
            for item in items or []:
                raw_display = str(item.get('display_name') or '').strip()
                category = str(
                    item.get('parent_name')
                    or item.get('category_name')
                    or ''
                ).strip()
                flavor = str(item.get('product_name') or '').strip()
                # 兼容旧的 ``品类(X)口味(Y)`` 预览字段。
                match = re.match(r'^品类\((.*?)\)口味\((.*?)\)$', raw_display)
                if match:
                    category = category or match.group(1).strip()
                    flavor = flavor or match.group(2).strip()
                category = category or '默认分类'
                flavor = flavor or raw_display or '-'
                group = groups.setdefault(category, {})
                group[flavor] = int(group.get(flavor) or 0) + int(item.get('quantity') or 0)
            return [
                f"{html.escape(category)}："
                + '、'.join(
                    f"{html.escape(flavor)}×{quantity}"
                    for flavor, quantity in flavors.items()
                )
                for category, flavors in groups.items()
            ]

        ok = order is not None and not error
        lines = ["✅ 录单成功" if ok else "❌ 录单失败", "", "订单信息："]
        if order:
            order_numbers = [
                str(number).strip()
                for number in (order.get('order_numbers') or [])
                if str(number).strip()
            ]
            if not order_numbers and order.get('order_number'):
                order_numbers = [str(order.get('order_number')).strip()]
            if order_numbers:
                escaped_numbers = '、'.join(
                    f"#{html.escape(number)}" for number in order_numbers
                )
                lines.append(f"订单号：{escaped_numbers}")
            is_split = bool(order.get('is_split')) or len(order_numbers) > 1
            if is_split:
                split_count = int(order.get('split_count') or len(order_numbers) or 1)
                lines.append(f"⚠️ 已自动拆为 {split_count} 笔，等待后台确认")
        # Committed API fields are authoritative; validated preview fields fill
        # presentation details omitted by compatible API versions.
        receipt = dict(preview)
        if order:
            receipt.update(order)
        lines.extend(compact_items(receipt.get('items') or []))
        lines.extend([
            f"姓名：{html.escape(str(receipt.get('recipient_name') or ''))}",
            f"手机号：{html.escape(str(receipt.get('recipient_phone') or ''))}",
            f"地址：{html.escape(str(receipt.get('recipient_address') or ''))}",
            f"商品金额：¥{float((order or preview).get('total_amount') or 0):.2f}",
            f"应付总额：¥{float((order or preview).get('grand_total') or 0):.2f}",
        ])
        if ok and preview.get('_recipient_generated'):
            lines.append('⚠️ 原文缺少收件人姓名，已使用管理员配置的临时收件名称。')
        if error:
            from order_receipt import customer_failure_reason
            lines.extend(["", f"失败原因：{html.escape(customer_failure_reason(error))}"])
        return '\n'.join(lines)

    async def _record_agent_group_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE, preview: Dict) -> None:
        """Create an order immediately for configured proxy groups."""
        items_payload = [{
            'product_id': item.get('product_id'),
            'product_name': item.get('product_name'),
            'parent_name': item.get('parent_name'),
            'quantity': item.get('quantity'),
            'warehouse_name': item.get('warehouse_name'),
        } for item in preview.get('items') or []]
        recipient_info = {
            'recipient_name': preview.get('recipient_name'),
            'recipient_phone': preview.get('recipient_phone'),
            'recipient_address': preview.get('recipient_address'),
        }
        success, order = await self._api_call(
            'create_order', telegram_id=preview.get('telegram_id'),
            items=items_payload, recipient_info=recipient_info,
            source_info={
                'source_chat_id': preview.get('source_chat_id'),
                'source_message_id': preview.get('source_message_id'),
                'source_chat_type': preview.get('source_chat_type'),
                'source_chat_title': preview.get('source_chat_title'),
            },
        )
        context.user_data.pop('pending_order', None)
        context.user_data.pop('pending_order_source', None)
        if not success:
            error = order.get('error') if isinstance(order, dict) else order
            text = self._agent_group_order_text(preview, error=str(error or '库存或地址校验失败'))
        else:
            # The legacy agent-group success formatter is a separate direct
            # receipt entry point; keep the same exact hold before any send.
            from six_unknown_receipt_hold import guard_direct
            from success_receipt_audit import identity
            guard_direct(identity(order),order_id=order.get('id'))
            try:
                from learning_pipeline import record_committed
                await asyncio.to_thread(record_committed, preview, order)
            except Exception as exc:
                logger.warning('committed learning deferred: %s', type(exc).__name__)
            text = self._agent_group_order_text(preview, order=order)
        if getattr(update, 'message', None):
            await self._reply_agent_group_message(update, text, parse_mode='HTML')
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode='HTML',
                reply_to_message_id=getattr(getattr(update, 'effective_message', None), 'message_id', None),
            )

    async def _send_agent_group_failure(self, update: Update, order_info: Dict, items: List[Dict], reason: str) -> None:
        preview = {
            'items': items or [
                {'display_name': str(ref), 'quantity': qty}
                for ref, qty in (order_info.get('items') or [])
            ],
            'recipient_name': order_info.get('recipient') or order_info.get('recipient_name'),
            'recipient_phone': order_info.get('phone') or order_info.get('recipient_phone'),
            'recipient_address': order_info.get('address') or order_info.get('recipient_address'),
        }
        await self._reply_agent_group_message(
            update, self._agent_group_order_text(preview, error=reason), parse_mode='HTML'
        )


    # ==================== 菜单/按钮 ====================
    BTN_PRODUCTS = "💰 单价查询"
    BTN_STOCK = "📊 库存预览"
    BTN_CART = "🛒 购物车"
    BTN_PROFILE = "👤 个人中心"
    BTN_ORDERS = "🧾 下单记录"
    BTN_SERVICE = "☎️ 联系客服"
    BTN_CUSTOM_BOT = "🛠 定制机器人"
    BTN_DROPSHIP = "🤝 申请代发"
    BTN_WHOLESALE_APPLY = "🤝 申请批发"
    BTN_ORDER = "📋 下单模板"
    BTN_HELP = "❓ 帮助"
    BTN_STATS = "📊 今日统计"
    BTN_BACK = "返回主菜单"

    SERVICE_USERNAMES = ("laomao_4321", "mengmeng_B31")  # 联系客服
    SERVICE_USERNAME = SERVICE_USERNAMES[0]  # 默认客服
    WHOLESALE_SERVICE_USERNAMES = SERVICE_USERNAMES
    CUSTOM_BOT_USERNAME = "m990606"  # 定制机器人
    PRODUCT_PAGE_SIZE = 10

    def _is_wholesale_bot(self) -> bool:
        return bool(IS_WHOLESALE_BOT or str(BOT_PROFILE or "").strip().lower() == "wholesale")

    def _apply_button_text(self) -> str:
        return self.BTN_WHOLESALE_APPLY if self._is_wholesale_bot() else self.BTN_DROPSHIP

    def _service_usernames(self) -> Tuple[str, ...]:
        return self.WHOLESALE_SERVICE_USERNAMES if self._is_wholesale_bot() else self.SERVICE_USERNAMES


    def _contact_inline_keyboard(self, kind: str = "service") -> InlineKeyboardMarkup:
        """一键跳转联系人。kind=service|custom_bot"""
        if kind == "custom_bot":
            username = self.CUSTOM_BOT_USERNAME
            title = f"打开 @{username} 定制机器人"
        else:
            rows = [
                [InlineKeyboardButton(f"打开 @{username} 联系客服", url=f"https://t.me/{username}")]
                for username in self._service_usernames()
            ]
            rows.append([InlineKeyboardButton(self.BTN_BACK, callback_data="menu:back")])
            return InlineKeyboardMarkup(rows)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(title, url=f"https://t.me/{username}")],
            [InlineKeyboardButton(self.BTN_BACK, callback_data="menu:back")],
        ])

    @staticmethod
    def _manual_review_keyboard(token=None) -> InlineKeyboardMarkup:
        """Actions shown when AI cannot safely turn a message into an order."""
        try:
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("取消订单", callback_data="manual_review:cancel" + (":"+token if token else "")),
                ],
            ])
        except (TypeError, ValueError):
            # Lightweight test doubles may not implement Telegram constructors.
            return None  # type: ignore[return-value]

    async def _queue_manual_review(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        raw_text: str,
        reasons: List[str],
        ai_result: Optional[Dict] = None,
    ) -> None:
        """Keep an AI rejection in the user's session until they request human review."""
        context.user_data['pending_manual_review'] = {
            'raw_text': str(raw_text or '')[:4000],
            'reasons': [str(reason) for reason in (reasons or [])[:8]],
            'ai_result': dict(ai_result or {}),
            'telegram_id': str(update.effective_user.id),
            'username': getattr(update.effective_user, 'username', None),
        }

    @staticmethod
    def _retire_noninteractive_failure(context) -> None:
        """Close recognition-only state without touching a committed order.

        A failed source stays in the durable source/job audit.  Only the live
        conversation state is removed so it cannot occupy a confirmation slot
        or be resumed as though the customer still owed a button click.
        """
        context.user_data.pop('pending_recipient_order', None)
        context.user_data.pop('pending_manual_review', None)

    async def handle_manual_review_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if self._is_group_chat(update) or len(str(update.callback_query.data or '').split(':'))==3:
            parts = str(update.callback_query.data or '').split(':')
            account = await self._api_call('get_telegram_account',telegram_id=str(update.effective_user.id))
            if len(parts)!=3 or not isinstance(account,dict) or not account.get('authorized'):
                await update.callback_query.answer('请选择对应草稿；接单群和资格必须有效',show_alert=True)
                return
            draft = await self._api_call('get_group_draft',update.effective_chat.id,parts[2],sender_id=str(update.effective_user.id))
            if not isinstance(draft,dict):
                await update.callback_query.answer('草稿已处理或已过期',show_alert=True)
                return
            from telegram_order_context import AccountUpdate,DraftContext
            scoped=DraftContext(context,draft)
            try:
                await self._handle_manual_review_callback(AccountUpdate(update,account,draft),scoped)
            finally:
                saved,_=await self._api_call('save_group_draft',update.effective_chat.id,draft['token'],scoped.user_data,
                                     not bool(scoped.user_data.get('pending_manual_review')),sender_id=str(update.effective_user.id))
                if not saved:raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')
            return
        if not await self._current_order_eligibility(update):
            await update.callback_query.answer('下单资格未通过',show_alert=True)
            return
        await self._handle_manual_review_callback(update,context)

    async def _handle_manual_review_callback(self,update,context):
        """Cancel rejected drafts or safely retire historical submit buttons."""
        query = update.callback_query
        await query.answer()
        pending = context.user_data.get('pending_manual_review') or {}
        if not pending:
            await query.edit_message_text('⚠️ 人工确认请求已过期，请重新发送订单。')
            return
        action = ':'.join(str(query.data or '').split(':')[:2])
        if action == 'manual_review:cancel':
            context.user_data.pop('pending_manual_review', None)
            await query.edit_message_text('已取消本次识别。')
            return
        if action != 'manual_review:submit':
            return
        # Old Telegram messages retain callback data after deployment. Never
        # forward or create an order from an obsolete generic submit action.
        await query.edit_message_text(
            '❌ 当前订单无法继续。请补充或修正信息后重新发送完整订单。',
            reply_markup=self._manual_review_keyboard(context.user_data.get('_group_draft_token')),
        )

    def _main_reply_keyboard(self, user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
        """All chats share the same user-collapsible main menu."""
        apply_btn = self._apply_button_text()
        if self._is_wholesale_bot():
            rows = [
                [KeyboardButton(self.BTN_PRODUCTS), KeyboardButton(self.BTN_STOCK)],
                [KeyboardButton(self.BTN_CART), KeyboardButton(self.BTN_PROFILE)],
                [KeyboardButton(self.BTN_ORDERS), KeyboardButton(self.BTN_SERVICE)],
                [KeyboardButton(apply_btn), KeyboardButton(self.BTN_ORDER)],
                [KeyboardButton(self.BTN_CUSTOM_BOT), KeyboardButton(self.BTN_HELP)],
            ]
        else:
            rows = [
                [KeyboardButton(self.BTN_PRODUCTS), KeyboardButton(self.BTN_STOCK)],
                [KeyboardButton(self.BTN_ORDERS), KeyboardButton(self.BTN_SERVICE)],
                [KeyboardButton(apply_btn), KeyboardButton(self.BTN_ORDER)],
            ]
        if user_id and self._is_admin(user_id):
            rows.append([KeyboardButton(self.BTN_STATS)])
        try:
            return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=False, one_time_keyboard=False)
        except TypeError:
            return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Explicit redisplay only; never alter or cancel an order draft."""
        await self._reply_with_menu(update,'请选择需要的功能。菜单可以手动收起，需要时发送 /menu 重新显示。')

    async def _reply_with_menu(self, update: Update, text: str, parse_mode: str = "HTML"):
        """统一回复：带底部菜单。"""
        user_id = update.effective_user.id if update.effective_user else None
        keyboard = self._main_reply_keyboard(user_id)
        if update.callback_query:
            # 回调查询时发新消息，避免破坏原确认卡片
            await update.callback_query.message.reply_text(
                text, parse_mode=parse_mode, reply_markup=keyboard
            )
        elif update.message:
            await update.message.reply_text(
                text, parse_mode=parse_mode, reply_markup=keyboard
            )


    @staticmethod
    def _split_message_chunks(text: str, limit: int = 3500) -> List[str]:
        raw = str(text or "")
        if len(raw) <= limit:
            return [raw]

        chunks: List[str] = []
        current = ""

        for line in raw.split("\n"):
            candidate = line if not current else current + "\n" + line
            if len(candidate) <= limit:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line

        if current:
            chunks.append(current)

        return chunks or [raw]

    async def _send_chunked_message(
        self,
        send_func,
        text: str,
        parse_mode: str = 'HTML',
        reply_markup=None,
        reply_to_message_id: Optional[int] = None,
    ):
        chunks = self._split_message_chunks(clean_telegram_text(text))
        for idx, chunk in enumerate(chunks):
            kwargs = {'parse_mode': parse_mode}
            if idx == 0 and reply_markup is not None:
                kwargs['reply_markup'] = reply_markup
            if idx == 0 and reply_to_message_id is not None:
                kwargs['reply_to_message_id'] = int(reply_to_message_id)
            await send_func(chunk, **kwargs)

    async def _setup_bot_menu(self, app: Application):
        """设置左侧菜单命令 + 聊天右下角菜单按钮。"""
        commands = [
            BotCommand("start", "开始使用 / 显示功能菜单"),
            BotCommand("menu", "重新显示功能菜单"),
            BotCommand("order", "下单模板"),
            BotCommand("stock", "库存预览"),
            BotCommand("products", "单价查询"),
            BotCommand("orders", "下单记录"),
            BotCommand("debt", "个人中心/欠款"),
            BotCommand("cancel", "取消尚未录入的订单草稿"),
            BotCommand("help", "帮助"),
            BotCommand("stats", "今日统计（管理员）"),
        ]
        await app.bot.set_my_commands(commands)
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as e:
            logger.warning(f"设置聊天菜单按钮失败: {e}")


    def _wholesale_order_guide_text(self) -> str:
        return "\n".join([
            "📌 <b>批发下单说明</b>",
            "",
            "第一步：联系客服，说明单量，了解批发价格。",
            "第二步：点击「申请批发」，客服在后台设置你的专属代发价格。",
            "第三步：查看库存，然后按格式下单。",
            "",
            "⚠️注意：出配货图结账，不召回不转寄。",
            "私人物流发货，下单请留一个能联系上的号码，否则容易丢件。因为虚假号码产生的问题概不负责.",
            "",
            "🧾 <b>下单格式</b>",
            "第一行：地址",
            "第二行：名字",
            "第三行：电话",
            "第四行：品名 / 型号",
            "下面每行：口味数量",
            "",
            "<code>下单",
            "北京市朝阳区xxx街道xxx号",
            "张三",
            "19999999999",
            "悦刻ACE",
            "冰爽铁观音x20",
            "浓郁铁观音20</code>",
            "",
            "也兼容旧格式：<code>品类(悦刻ACE)口味(冰爽铁观音)x20</code>",
            "多商品可点「🛒 购物车」按型号挑口味、调数量后一起结算；单价查询仅展示品类价格。",
            "",
            "联系客服：@laomao_4321 / @mengmeng_B31",
        ])


    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/start：展示欢迎模板，并固定显示聊天底部键盘按钮。"""
        user = update.effective_user
        user_id = user.id if user else None
        nl = chr(10)
        if self._is_wholesale_bot():
            text = self._wholesale_order_guide_text()
        else:
            text = nl.join([
                "📌 <b>操作指南</b>",
                "",
                "第一步：联系客服，说明每日订单量，了解代发价格。",
                "第二步：点击「申请代发」，客服在后台设置你的专属代发价格。",
                "第三步：查看库存预览，然后按格式下单。",
                "⚠️注意：下单品品类名称和口味名称尽量和商品预览时的名称一样！",
                "🧾 <b>下单格式</b>",
                "<code>下单",
                "北京市朝阳区xxx街道xxx号",
                "张三",
                "19999999999",
                "品类(鸭子)口味(苹果)1",
                "品类(新鸭子)口味(西瓜)2</code>",
                "",
                "下单时可在订单内容中填写品类和口味。",
            ])
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=self._main_reply_keyboard(user_id),
        )


    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/help 命令处理"""
        if self._is_wholesale_bot():
            await update.message.reply_text(
                self._wholesale_order_guide_text(),
                parse_mode='HTML',
                reply_markup=self._main_reply_keyboard(update.effective_user.id if update.effective_user else None),
            )
            return
        help_text = """📚 <b>使用帮助</b>

<b>✨ 系统特点：</b>
• <b>智能仓库匹配</b> - 可整单履约的仓库中，优先选择当天该品类已分配商品件数较少的仓库；件数相同时按可用库存，再按稳定仓库排序选择
• <b>详细错误提示</b> - 准确告知问题原因和解决方案
• <b>仓库运费计算</b> - 按最终履约仓库及商品品类计算运费，报价变化时需重新确认

<b>下单步骤：</b>
1️⃣ 发送订单信息（商品、地址、收件人、电话）
2️⃣ 系统自动提取并验证信息
3️⃣ 自动匹配可整单履约的仓库并展示报价
4️⃣ 检查库存和余额
5️⃣ 确认最终报价后创建订单并返回详细信息

<b>📋 下单格式示例：</b>
```
商品: 苹果 x2, 香蕉 x1, 橙子 x3
地址: 北京市朝阳区建国路88号
收件人: 张三
电话: 13800138000
```

<b>📝 支持多种格式：</b>
• <code>苹果 x2</code> 或 <code>苹果2个</code>
• <code>商品:</code> 标签可省略
• 可以写<code>购买:</code>、<code>物品:</code>等

<b>⚠️ 注意事项：</b>
• 商品名称必须准确（使用 /products 查看列表）
• 地址越详细越好，避免识别错误
• 可用 /debt 查看欠款，不强制每单一结
• 如果某商品在所有仓库库存都不足，订单将失败
• 一笔订单由同一仓库完整履约；没有仓库可完整履约时会提示无法下单

<b>💡 常见问题：</b>
• <i>订单失败？</i> 查看错误提示，会明确告知原因
• <i>怎么结账？</i> 人工收账（人民币/USDT），后台可查欠款
• <i>商品缺货？</i> 使用 /stock 查看库存\n• <i>下单流程？</i> 发送订单后先预览，确认后生成订单并减库存（人工收账）

<b>🔧 可用命令：</b>
"""
        for cmd, desc in COMMANDS.items():
            help_text += f"/{cmd} - {desc}\n"

        await update.message.reply_text(help_text, parse_mode='HTML', reply_markup=self._main_reply_keyboard(update.effective_user.id if update.effective_user else None))

    async def chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Return the current Telegram chat ID for agent-group setup."""
        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return
        chat_id = getattr(chat, 'id', None)
        chat_type = getattr(chat, 'type', '') or ''
        title = getattr(chat, 'title', None) or getattr(chat, 'username', None) or 'private chat'
        await message.reply_text(
            f"Current chat: {title}\nType: {chat_type}\nChat ID: <code>{chat_id}</code>",
            parse_mode='HTML',
        )

    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """兼容旧命令：跳转到欠款/账户信息"""
        await self.debt(update, context)

    async def _legacy_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/products 显示商品及当前用户结算单价"""
        telegram_id = str(update.effective_user.id)
        success, user = await self._api_call(
            'create_or_get_user',
            telegram_id,
            update.effective_user.username,
            getattr(update.effective_user, 'full_name', None),
        )
        warehouses = self._visible_warehouses(
            await self._api_call('get_all_warehouses'), update=update, user=user,
        )
        products = self._visible_products(await self._api_call('get_products'), warehouses)
        if not products:
            await update.message.reply_text("暂无商品信息")
            return

        price_map = {}
        if success and user:
            detail = await self._api_call('get_user_prices', user['id'])
            for item in detail.get('prices', []):
                price_map[item['product_id']] = item

        lines = ["🛍 <b>商品列表 / 你的结算单价</b>", ""]
        for product in sorted(products, key=lambda x: x['name']):
            info = price_map.get(product['id'])
            if info:
                tag = "专属" if info.get('is_custom') else "默认"
                lines.append(f"• {product['name']} - ¥{float(info['unit_price']):.2f}/个 ({tag})")
            else:
                lines.append(f"• {product['name']} - ¥{float(product.get('price') or 0):.2f}/个")
        lines.append("")
        lines.append("💡 单价由后台按用户单独设置；下单按你的单价记账")
        await self._safe_reply(update, chr(10).join(lines), parse_mode='HTML')

    async def products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/products 只显示当前用户按品类汇总后的结算单价。"""
        telegram_id = str(update.effective_user.id)
        success, user = await self._api_call(
            'create_or_get_user',
            telegram_id,
            update.effective_user.username,
            getattr(update.effective_user, 'full_name', None),
        )
        warehouses = self._visible_warehouses(
            await self._api_call('get_all_warehouses'), update=update, user=user,
        )
        products = self._visible_products(await self._api_call('get_products'), warehouses)
        if not products:
            await update.message.reply_text("暂无商品信息")
            return

        price_map = {}
        if success and user:
            detail = await self._api_call('get_user_prices', user['id'])
            for item in (detail or {}).get('prices', []):
                price_map[item.get('product_id')] = item

        lines = ["💰 <b>单价查询</b>", ""]
        import sales_pause
        import config
        from html import escape
        config.load_runtime_settings()
        warehouses = sales_pause.active_warehouses(getattr(config, 'SALES_PAUSE_SECTION', None), warehouses)
        for warehouse_name, rows in build_warehouse_price_sections(products,warehouses,price_map):
            lines.append(f"【{escape(warehouse_name)}】")
            for category, price_text in rows:
                lines.append(f"• {escape(category)}：{price_text}")
            lines.append('')
        await self._safe_reply(update, chr(10).join(lines), parse_mode='HTML')

    async def stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/stock 库存预览：发送文字清单，并按仓库附上实时库存图。"""
        args = getattr(context, "args", None) or []
        products, warehouses, _user = await self._visible_catalog_for_user_async(update)
        # Compatibility fallback for embedded/local API fixtures that expose
        # only the legacy synchronous catalog helper.
        if not products and not warehouses:
            try:
                products, warehouses, _user = (await self._visible_catalog_for_user_async(update))
            except Exception:
                products, warehouses = [], []
        if not self._is_wholesale_bot():
            import sales_pause
            import config
            config.load_runtime_settings()
            warehouses=sales_pause.active_warehouses(getattr(config,'SALES_PAUSE_SECTION',None),warehouses)
            try:
                stocks=await self._api_call('get_inventory_image_rows')
            except Exception:
                logger.warning('inventory_gallery catalog_read_failed')
                await self._safe_reply(update,'库存预览暂时无法生成，请稍后重试或联系客服。')
                return
            if stocks is None:
                # Compatibility for old embedded test clients only.
                stocks=await self._api_call('get_stock',include_inactive=True) or []
            allowed={int(w['id']) for w in warehouses}
            stocks=[r for r in stocks if int(r.get('warehouse_id') or 0) in allowed]
            product_map={int(p['id']):p for p in products}
            for row in stocks:
                meta=product_map.get(int(row.get('product_id') or 0),{})
                row.setdefault('parent_id',meta.get('parent_id'))
                row.setdefault('parent_name',meta.get('parent_name'))
            await self._send_inventory_gallery(update,stocks,warehouses)
            return
        stocks = filter_visible_stock_rows(
            products,
            warehouses,
            await self._api_call('get_stock', include_inactive=True) or [],
        )
        if not stocks:
            await self._safe_reply(update, "暂无库存信息")
            return

        filtered = stocks
        for arg in args:
            arg = str(arg).strip()
            if not arg:
                continue
            filtered = [
                s for s in filtered
                if arg in str(s.get('product_name') or '')
                or arg in str(s.get('parent_name') or '')
                or arg in str(s.get('warehouse_name') or '')
            ]

        if not filtered:
            await self._safe_reply(update, "❌ 未找到匹配的库存记录")
            return

        grouped: Dict[str, Dict[int, Dict]] = {}
        for s in filtered:
            model = s.get('parent_name') or '默认型号'
            pid = int(s.get('product_id') or 0)
            if not pid:
                continue
            item = grouped.setdefault(model, {}).setdefault(pid, {
                'name': s.get('product_name') or f'口味{pid}',
                'quantity': 0,
                'threshold': normalize_threshold(s.get('low_stock_threshold')),
            })
            item['quantity'] = int(s.get('quantity') or 0)
            item['threshold'] = normalize_threshold(s.get('low_stock_threshold'), item.get('threshold', 20))

        # 库存预览只展示可用商品；零库存商品仍由下单校验负责拦截。
        show_out_of_stock = False
        lines = [
            "📊 <b>库存预览</b>",
            (
                "🟢 正常  🟡 少量  🔴 缺货"
                if show_out_of_stock
                else "🟢 正常  🟡 少量  （缺货已隐藏）"
            ),
            "少量阈值可在后台商品管理里单独设置。",
            "",
        ]
        def model_sort(entry):
            model_name, model_items = entry
            has_available = any(
                not is_stock_paused(int(item.get('quantity') or 0), normalize_threshold(item.get('threshold')))
                for item in model_items.values()
            )
            total_qty = sum(int(item.get('quantity') or 0) for item in model_items.values())
            return (0 if has_available else 1, -total_qty, str(model_name))

        def item_sort(item):
            qty = int(item.get('quantity') or 0)
            threshold = normalize_threshold(item.get('threshold'))
            paused = is_stock_paused(qty, threshold)
            return (0 if not paused else 1, -qty, str(item.get('name') or ''))

        rendered_items = False
        for model, items in sorted(grouped.items(), key=model_sort):
            item_lines = []
            for item in sorted(items.values(), key=item_sort):
                qty = int(item.get('quantity') or 0)
                threshold = normalize_threshold(item.get('threshold'))
                if qty <= 0:
                    if not show_out_of_stock:
                        continue
                    flag = "🔴"
                    status = "缺货"
                elif threshold > 0 and qty <= threshold:
                    flag = "🟡"
                    status = f"少量≤{threshold}"
                else:
                    flag = "🟢"
                    status = "正常"
                if is_stock_paused(qty, threshold):
                    status += "，暂停售卖"
                item_lines.append(f"{flag} 口味 {item['name']}：{qty}（{status}）")
            if not item_lines:
                continue
            rendered_items = True
            lines.append(f"型号：<b>{model}</b>")
            lines.extend(item_lines)
            lines.append("")
        if not rendered_items:
            await self._safe_reply(update, "暂无有货商品")
            return
        message = chr(10).join(lines).strip()
        await self._safe_reply(update, message)
        # The wholesale snapshot orders products by creation time and id.  The
        # stock endpoint omits creation time, so enrich only the image rows
        # from the already-authorized visible catalog without changing text.
        product_meta = {
            int(product.get('id') or 0): product for product in products
            if int(product.get('id') or 0)
        }
        image_rows = []
        for row in filtered:
            image_row = dict(row)
            meta = product_meta.get(int(row.get('product_id') or 0), {})
            image_row['created_at'] = meta.get('created_at') or ''
            image_row['parent_id'] = row.get('parent_id') or meta.get('parent_id') or ''
            image_rows.append(image_row)
        await self._send_inventory_preview_images(update, image_rows)

    async def _send_inventory_gallery(self,update,rows,warehouses):
        """Fresh warehouse snapshots, image-only; a send failure never replays business."""
        from inventory_preview import InventorySnapshot
        from telegram import InputMediaDocument,InputFile
        message=getattr(update,'effective_message',None) or getattr(update,'message',None)
        if message is None:return False
        snapshots={s.warehouse_id:s for s in snapshots_from_stock_rows(rows,include_zero=True)}
        media=[]
        cache=os.path.join(os.path.dirname(os.path.abspath(__file__)),'cache')
        for warehouse in warehouses:
            wid=str(warehouse['id'])
            snapshot=snapshots.get(wid) or InventorySnapshot((),wid,warehouse.get('name','仓库'))
            try:
                settings=inventory_image_settings(warehouse_id=wid)
                # This command is now image-only, including warehouses whose old
                # text+image preference was disabled. Do not persist this override.
                settings=dict(settings,inventory_image_enabled='1')
                from inventory_preview import render_inventory_image_pages_bytes
                pages,_=await asyncio.to_thread(render_inventory_image_pages_bytes,snapshot,settings,cache)
                for page_index,content in enumerate(pages,1):
                    suffix=f'-第{page_index}页' if len(pages)>1 else ''
                    media.append(InputMediaDocument(InputFile(content,filename=snapshot.warehouse_name+'-实时库存'+suffix+'.jpg',attach=True)))
            except Exception:
                logger.warning('inventory_gallery generation_failed warehouse=%s',wid)
        if not media:
            await self._safe_reply(update,'库存预览暂时无法生成，请稍后重试或联系客服。')
            return False
        sent=False
        for offset in range(0,len(media),10):
            chunk=media[offset:offset+10]
            transaction=f"{getattr(update,'update_id',None) or id(update)}:gallery:{offset}"
            try:
                with inventory_document_transaction(transaction):
                    if len(chunk)==1:
                        await message.reply_document(document=chunk[0].media)
                    else:
                        await message.reply_media_group(media=chunk)
                sent=True
            except Exception:
                # Timeout may already have delivered; never fan out/retry here.
                logger.warning('inventory_gallery delivery_uncertain batch=%s',offset)
        return sent

    async def _send_inventory_preview_images(self, update: Update, stock_rows: List[Dict]) -> bool:
        """Best-effort image enhancement; the text preview has already been sent."""
        message = getattr(update, "message", None)
        callback_query = getattr(update, "callback_query", None)
        if message is None and callback_query is not None:
            message = getattr(callback_query, "message", None)
        if message is None:
            return False

        # Match the wholesale image rule: keep zero-stock rows in the image so
        # they receive the red status dot.  The text preview above deliberately
        # keeps its existing behavior and continues to hide unavailable items.
        snapshots = snapshots_from_stock_rows(stock_rows, include_zero=True)
        if not snapshots:
            return False
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        sent = False
        for snapshot in snapshots:
            try:
                settings = inventory_image_settings(warehouse_id=snapshot.warehouse_id)
                if settings.get('inventory_image_enabled') != '1':
                    continue
                from inventory_preview import render_inventory_image_pages_bytes
                pages,_cache_hit=await asyncio.to_thread(render_inventory_image_pages_bytes,snapshot,settings,cache_dir)
                for page_index,content in enumerate(pages,1):
                    transaction=(f"{getattr(update,'update_id',None) if getattr(update,'update_id',None) is not None else id(update)}"
                                 f":{snapshot.warehouse_id}:page{page_index}")
                    suffix=f'-第{page_index}页' if len(pages)>1 else ''
                    with inventory_document_transaction(transaction):
                        # Keep the existing document sender and replay protection;
                        # only the measured JPEG page list is expanded.
                        await message.reply_document(document=content,
                            filename=f"{snapshot.warehouse_name or '库存'}-实时库存{suffix}.jpg")
                    sent=True
            except Exception as exc:
                logger.warning(
                    "库存预览图片生成失败 warehouse=%s error=%s",
                    snapshot.warehouse_id,
                    clean_telegram_text(exc),
                )
        return sent

    async def orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        account=str(update.effective_user.id)
        summary=await self._api_call('account_summary',account)
        if not isinstance(summary,dict):
            await update.effective_message.reply_text('账务摘要暂时不可用，请稍后重试。')
            return
        lines=['🧾 下单记录',f"• 预存余额: ¥{summary['prepaid']:.2f}",
            f"• 今日下单: {summary['today_count']}单 / ¥{summary['today_amount']:.2f}",
            f"• 本周账单: ¥{summary['week_amount']:.2f}",f"• 已结金额: ¥{summary['settled']:.2f}",
            f"• 待结账: ¥{summary['pending']:.2f}"]
        rows=await self._api_call('account_order_search',account)
        lines.extend(self._account_order_lines(rows or []))
        await self._send_chunked_message(update.effective_message.reply_text,'\n'.join(lines),parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔍 搜索订单',callback_data='account:search')]]))

    @staticmethod
    def _account_order_lines(orders):
        lines=[]
        for order in orders:
            lines.extend(['',html.escape(str(order['order_number']))+' · '+format_beijing_time(order.get('created_at')),
                html.escape(str(order.get('recipient_name') or ''))+' / '+html.escape(str(order.get('recipient_phone') or '')),
                html.escape(str(order.get('recipient_address') or ''))])
            groups={}
            total=0
            for item in order.get('items') or []:
                groups.setdefault(item.get('parent_name') or '默认分类',[]).append(str(item.get('product_name') or '')+'×'+str(item.get('quantity') or 0))
                total+=int(item.get('quantity') or 0)
            lines.extend(html.escape(category+'：'+'、'.join(items)) for category,items in groups.items())
            from product_units import total_label
            lines.append(total_label(order.get('items') or []))
            from order_status_display import business_status
            lines.append(f"¥{float(order.get('grand_total') or 0):.2f} / "+business_status(order))
            if order.get('waybill'):
                lines.append('物流：'+html.escape(str(order.get('carrier') or '')+' '+str(order['waybill'])))
        return lines

    async def handle_account_callback(self,update,context):
        query=update.callback_query
        await query.answer()
        if query.data=='account:search':
            context.user_data['account_search_pending']=True
            context.user_data['account_search_chat_id']=str(update.effective_chat.id)
            await query.message.reply_text('请输入收件人姓名、手机号或地址关键词。',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('取消搜索',callback_data='account:cancel')]]))
        elif query.data=='account:cancel':
            context.user_data.pop('account_search_pending',None)
            context.user_data.pop('account_search_chat_id',None)
            await self.orders(update,context)
        elif query.data.startswith('account:next:'):
            await self._account_search_result(update,context,context.user_data.get('account_search_keyword',''),int(query.data.rsplit(':',1)[1]))

    async def _account_search_result(self,update,context,keyword,offset=0):
        context.user_data.pop('account_search_pending',None)
        context.user_data['account_search_keyword']=keyword
        rows=await self._api_call('account_order_search',str(update.effective_user.id),keyword,offset)
        if rows is None:
            await update.effective_message.reply_text('搜索暂时不可用，请稍后重试。');return
        keyboard=[]
        if len(rows)==5:keyboard.append([InlineKeyboardButton('下一页',callback_data='account:next:'+str(offset+5))])
        keyboard.append([InlineKeyboardButton('返回下单记录',callback_data='account:cancel')])
        await self._send_chunked_message(update.effective_message.reply_text,'\n'.join(self._account_order_lines(rows)) if rows else '没有找到自己的匹配订单。',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

    async def _legacy_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/orders 展示状态、支付与待收"""
        user_id = str(update.effective_user.id)
        user_orders = await self._api_call('get_user_orders', user_id)
        if not user_orders:
            await update.message.reply_text('您还没有订单记录')
            return
        try:
            user_orders = sorted(user_orders, key=lambda x: x.get('created_at') or '', reverse=True)
        except Exception:
            pass
        recent_orders = user_orders[:5]
        lines = ['📋 <b>您的订单历史</b>', '']
        status_map = {
            'pending': '⏳待处理',
            'confirmed': '📝已确认',
            'shipped': '🚚已发货',
            'cancelled': '❌已取消',
        }
        pay_map = {
            'unpaid': '未收款',
            'partial': '部分收款',
            'paid': '已结清',
        }
        for order in recent_orders:
            items = order.get('items') or []
            items_summary = ', '.join([
                str(item.get('product_name') or '') + 'x' + str(item.get('quantity') or 0)
                for item in items
            ])
            status_text = status_map.get(order.get('status'), order.get('status'))
            pay_status = order.get('payment_status') or 'unpaid'
            pay_text = pay_map.get(pay_status, pay_status)
            total = float(order.get('grand_total') or order.get('total_amount') or 0)
            paid = float(order.get('paid_amount') or 0)
            due = order.get('due_amount')
            if due is None:
                due = max(0.0, total - paid)
            lines.append('🆔 #' + str(order.get('order_number')) + ' - ¥' + format(total, '.2f'))
            lines.append('   下单时间（北京时间）：' + format_beijing_time(order.get('created_at')))
            if items_summary:
                lines.append('   ' + items_summary)
            lines.append('   ' + str(status_text) + ' | ' + str(pay_text) + ' | 待收 ¥' + format(float(due), '.2f'))
            lines.append('')
        lines.append('💡 用 /debt 查看总欠款；未发货可用 /cancel 订单号提交取消申请（等待后台审核）')
        await update.message.reply_text(chr(10).join(lines), parse_mode='HTML')
    def _is_admin(self, user_id: int) -> bool:
        """管理员判断：未配置管理员时默认放行（便于本地测试）"""
        if not ADMIN_TELEGRAM_IDS:
            return True
        return user_id in ADMIN_TELEGRAM_IDS

    @staticmethod
    def _username_token(value: Optional[str]) -> str:
        return str(value or '').strip().lstrip('@').casefold()

    @classmethod
    def _private_warehouse_name(cls) -> str:
        import config as cfg
        return str(getattr(cfg, 'WHOLESALE_PRIVATE_WAREHOUSE_NAME', '') or '').strip()

    @classmethod
    def _private_username(cls) -> str:
        import config as cfg
        return cls._username_token(getattr(cfg, 'WHOLESALE_PRIVATE_USERNAME', 'xiaolin005'))

    @classmethod
    def _is_private_warehouse(cls, warehouse: Optional[Dict]) -> bool:
        return bool(
            cls._private_warehouse_name()
            and str((warehouse or {}).get('name') or '').strip() == cls._private_warehouse_name()
        )

    @classmethod
    def _can_access_private_warehouse(cls, update: Optional[Update] = None, user: Optional[Dict] = None) -> bool:
        telegram_user = getattr(update, 'effective_user', None) if update else None
        username = getattr(telegram_user, 'username', None)
        if username is None and user:
            username = user.get('username')
        return bool(cls._private_username() and cls._username_token(username) == cls._private_username())

    @classmethod
    def _visible_warehouses(cls, warehouses: List[Dict], update: Optional[Update] = None, user: Optional[Dict] = None) -> List[Dict]:
        rows = [row for row in (warehouses or []) if row.get('is_active', True)]
        if cls._can_access_private_warehouse(update, user):
            return rows
        return [row for row in rows if not cls._is_private_warehouse(row)]

    @classmethod
    def _visible_products(cls, products: List[Dict], warehouses: List[Dict]) -> List[Dict]:
        allowed_ids = {int(row.get('id')) for row in (warehouses or []) if row.get('id') is not None}
        visible = []
        for product in products or []:
            try:
                default_id = int(product.get('default_warehouse_id') or 0)
            except (TypeError, ValueError):
                default_id = 0
            if default_id and default_id in allowed_ids:
                visible.append(product)
        return visible

    def _visible_catalog_for_user(self, update: Update):
        user = None
        telegram_user = getattr(update, 'effective_user', None)
        if telegram_user:
            _ok, user = api_client.create_or_get_user(
                str(telegram_user.id),
                getattr(telegram_user, 'username', None),
                getattr(telegram_user, 'full_name', None),
            )
        warehouses = self._visible_warehouses(api_client.get_all_warehouses(), update=update, user=user)
        products = self._visible_products(api_client.get_products(), warehouses)
        return products, warehouses, user

    def _match_warehouse(self, name: Optional[str], warehouses: List[Dict]) -> Optional[Dict]:
        if not warehouses:
            return None
        if not name:
            return next((w for w in warehouses if w.get('is_active', True)), warehouses[0])
        for w in warehouses:
            if w.get('name') == name:
                return w
        for w in warehouses:
            if name in w.get('name', '') or w.get('name', '') in name:
                return w
        return None

    @__import__('order_trace').timed_sync('CATALOG_MATCH')
    def _match_product(self, name: str, products: List[Dict]) -> Optional[Dict]:
        model, flavor = self._split_product_reference(name)
        from brand_aliases import scope_catalog
        scoped = scope_catalog(model, products)
        if scoped is not None:
            products = scoped
            model = scoped[0]['parent_name'] if scoped else model
        if model and flavor:
            from rule_dictionary import manual_flavor
            canonical = self._canonical_catalog_category(model, products)
            pool = [p for p in products if p.get('parent_name') == canonical]
            try:
                alias = manual_flavor(canonical, flavor, [p.get('name') for p in pool], pool)
            except ValueError:
                return None
            if alias:
                hits = [p for p in pool if p.get('name') == alias]
                return hits[0] if len(hits) == 1 else None
        if flavor:
            exact = [
                p for p in products
                if str(p.get('name') or '') == flavor
                and (not model or self._model_matches(p.get('parent_name'), model))
            ]
            if len(exact) == 1:
                return exact[0]
        # Ambiguous exact names and partial/fuzzy names require confirmation.
        # Never escape an explicit category or choose the first other brand.
        return None

    def _split_product_reference(self, name: str) -> Tuple[Optional[str], str]:
        text = str(name or '').strip()
        patterns = [
            r"(?:品类|型号)\(([^)]+)\)\s*口味\(([^)]+)\)",
            r"(?:品类|型号)[:：]?\s*([^\s,，]+)\s+口味[:：]?\s*([^\s,，]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return m.group(1).strip(), m.group(2).strip()
        # Explicit source separator, not category inference. Preserve the
        # category even when an abbreviated flavor needs confirmation.
        separated=re.fullmatch(r'([^:：()（）]+?)\s*[:：]\s*([^:：]+)',text)
        if separated:
            return separated[1].strip(),separated[2].strip()
        parts = text.split()
        if len(parts) >= 2:
            return parts[0].strip(), parts[-1].strip()
        return None, text

    @staticmethod
    def _normalize_product_token(text: str) -> str:
        value = re.sub(r"^(?:品类|型号)[:：]?\s*", "", str(text or '').strip())
        value = re.sub(r"[()（）\s]+", "", value)
        return value.lower()

    @classmethod
    def _model_matches(cls, candidate_model: Optional[str], ref_model: Optional[str]) -> bool:
        candidate = cls._normalize_product_token(candidate_model)
        ref = cls._normalize_product_token(ref_model)
        if not candidate or not ref:
            return False
        return candidate == ref

    @staticmethod
    def _normalize_catalog_token(value: Optional[str]) -> str:
        # 客户常把组合口味写成“草莓+西瓜”，目录可能存成“草莓西瓜”；
        # 比较目录/客户写法时统一移除连接符。
        return re.sub(r"[\s\-_—/+＋&和、,，（）()]+", "", str(value or "")).casefold()

    def _live_parent_names(self, products: List[Dict]) -> List[str]:
        """当前目录里的品类名（去重、保序）。"""
        names = []
        seen = set()
        for product in products or []:
            name = str(product.get("parent_name") or "").strip()
            token = self._normalize_catalog_token(name)
            if name and token and token not in seen:
                seen.add(token)
                names.append(name)
        return names

    def _canonical_catalog_category(
        self,
        model: Optional[str],
        products: List[Dict],
    ) -> Optional[str]:
        """手工下单写的型号词也走 deepseek_resolver，两条路径共用同一套判据。

        必须这么做的原因：`_model_matches` 是前缀匹配，「板砖2W」里 `板砖2w`
        以 `板砖` 开头，于是二代写法被静默算成一代，发错货且不报错。
        解析不出来时返回 None，调用方保留客户原写法（行为与改造前一致）。
        """
        text = str(model or "").strip()
        if not text:
            return None
        live = self._live_parent_names(products)
        if not live:
            return None
        try:
            from brand_aliases import scope_catalog
            scoped = scope_catalog(text, products)
            if scoped is not None:
                if not scoped:
                    return None
                return scoped[0]['parent_name']
            canonical, _reason = resolve_catalog_category(text, live)
        except Exception:
            return None
        return canonical

    @staticmethod
    def _ai_text_token(value: Optional[str]) -> str:
        """Normalize free text for conservative source-evidence checks."""
        # Audited Emoji aliases are source evidence too. Keep Unicode symbols,
        # while still removing separators/punctuation; catalog validation follows.
        return ''.join(c for c in str(value or '') if c.isalnum() or
                       unicodedata.category(c) in ('So', 'Sk')).casefold()

    @classmethod
    def _ai_item_has_source_evidence(
        cls,
        raw_text: str,
        category: Optional[str],
        flavor: str,
        source_text: Optional[str] = None,
    ) -> bool:
        """Reject model-only products that cannot be found in the customer text."""
        from contact_fields import merchandise_text
        raw_token = cls._ai_text_token(merchandise_text(raw_text))
        if not raw_token:
            return False
        flavor_token = cls._ai_text_token(flavor)
        category_token = cls._ai_text_token(category)
        evidence_token = cls._ai_text_token(source_text)
        if evidence_token and evidence_token in raw_token:
            if flavor_token and flavor_token not in evidence_token:
                return False
            if category_token and category_token not in raw_token:
                return False
            return True

        if flavor_token and flavor_token in raw_token:
            if not category_token or category_token in raw_token:
                return True

        # A catalog combination such as "崧A66+双响炮" may be written with
        # spaces or punctuation in the source while remaining one product.
        parts = [
            cls._ai_text_token(part)
            for part in re.split(r"[+＋&和、,，/／\s]+", str(flavor or ""))
            if cls._ai_text_token(part)
        ]
        if len(parts) > 1 and all(part in raw_token for part in parts):
            return not category_token or category_token in raw_token
        return False

    @classmethod
    def _ai_item_evidence_ok(
        cls,
        raw_text: str,
        item: Dict,
        require_category: bool,
        products=(),
    ) -> Tuple[bool, str]:
        """用「客户自己的原话」核对商品是否真的被提到过。

        旧做法拿目录规范名去原文里找，客户写「松a29」而目录是「崧A29」就永远
        找不到，把识别正确的单也拒了。现在核对的是模型抄回来的客户原话
        （raw_category / raw_flavor），防幻觉能力不变：原话必须逐字出现在原文里，
        模型编不出原文没有的词。
        """
        from contact_fields import merchandise_text
        raw_token = cls._ai_text_token(merchandise_text(raw_text, products=products))
        if not raw_token:
            return False, '原文为空'

        flavor_word = item.get('raw_flavor') or item.get('flavor')
        flavor_token = cls._ai_text_token(flavor_word)
        if not flavor_token:
            return False, '缺少口味原话'
        if flavor_token not in raw_token:
            return False, f'原文没有口味原话({flavor_word})'

        source_text = item.get('source_text')
        source_token = cls._ai_text_token(source_text)
        if source_token and source_token not in raw_token:
            from source_evidence import actual_span
            actual=actual_span(raw_text,str(source_text))
            if actual is None:
                return False, f'source_text 不是原文片段({source_text})'
            item['source_text']=actual

        if require_category:
            category_word = item.get('raw_category') or item.get('category')
            category_token = cls._ai_text_token(category_word)
            if not category_token:
                return False, '口味跨品类多义但缺少型号原话'
            if category_token not in raw_token:
                return False, f'原文没有型号原话({category_word})'
        return True, ''

    def _build_deepseek_catalog(self, products: List[Dict]) -> List[Dict[str, str]]:
        """Create a deduplicated, name-only catalog for the extraction prompt."""
        catalog = []
        seen = set()
        for product in products or []:
            category = str(product.get('parent_name') or '').strip()
            flavor = str(product.get('name') or '').strip()
            if not flavor:
                continue
            key = (self._normalize_catalog_token(category), self._normalize_catalog_token(flavor))
            if key in seen:
                continue
            seen.add(key)
            catalog.append({'category': category or None, 'flavor': flavor})
        return catalog

    def _unresolved_reply_text(self, unresolved: List[Dict]) -> str:
        """把「目录里没这个口味」翻译成客户看得懂的话，并列出可选口味。"""
        from html import escape
        lines = ['❌ 录单失败，以下商品未找到，本次完整订单未录入：', '']
        for entry in unresolved:
            raw_category = escape(str(entry.get('raw_category') or '未写型号'))
            raw_flavor = escape(str(entry.get('raw_flavor') or '未写口味'))
            category = entry.get('category')
            options = entry.get('options') or []
            count=entry.get('quantity')
            suffix=(' ×'+str(count)) if type(count) is int and count>0 else ''
            if category and options:
                lines.append(f'• <b>{raw_category}</b> 没有「{raw_flavor}」这个口味{suffix}')
                lines.append(f'  {escape(str(category))} 目前可选：{escape("、".join(map(str,options[:30])))}')
            elif category:
                lines.append(f'• <b>{raw_category}</b> 没有「{raw_flavor}」这个口味{suffix}')
            else:
                lines.append(f'• 没找到「{raw_category}」这个型号（口味写的是「{raw_flavor}」{suffix}）')
            lines.append('')
        lines.append('请确认下品类名称有没有错，没有错的话麻烦按照上面的口味名称改下。')
        return '\n'.join(lines)

    async def _push_aftersale_to_service(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        raw_text: str,
        ai_result: Optional[Dict] = None,
    ) -> bool:
        """把售后 / 补发单推给客服。返回是否至少推送成功一个。

        客服只有 Telegram 用户名，机器人无法靠用户名主动发消息，所以这里用
        AFTERSALE_NOTIFY_IDS 里的数字 ID（留空则回退到管理员）。对方必须先和
        机器人对话过一次，否则 Telegram 会拒绝投递。
        """
        targets = list(AFTERSALE_NOTIFY_IDS) or list(ADMIN_TELEGRAM_IDS)
        if not targets:
            logger.warning('售后单无处推送：AFTERSALE_NOTIFY_IDS 和 ADMIN_TELEGRAM_IDS 都为空')
            return False

        user = update.effective_user
        who = f'@{user.username}' if user.username else (getattr(user, 'full_name', '') or '')
        parts = [
            '🔧 <b>售后 / 补发单</b>',
            '',
            f'客户：{who}（ID {user.id}）',
        ]
        if ai_result:
            recipient = ai_result.get('recipient') or '—'
            phone = ai_result.get('phone') or '—'
            address = ai_result.get('address') or '—'
            parts += [f'收件人：{recipient}', f'电话：{phone}', f'地址：{address}']
            items = ai_result.get('items') or []
            if items:
                parts.append('商品：')
                for item in items[:20]:
                    parts.append(
                        f'  · {item.get("category") or ""} {item.get("flavor") or ""}'
                        f' x{item.get("quantity")}'
                    )
            notes = ai_result.get('notes') or []
            if notes:
                parts.append('备注：' + '；'.join(str(x) for x in notes[:5]))
        parts += ['', '<b>客户原文：</b>', html.escape(str(raw_text or '')[:1500])]
        text = '\n'.join(parts)

        delivered = 0
        for chat_id in targets:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
                delivered += 1
            except Exception as exc:
                logger.warning('推送售后单给 %s 失败: %s', chat_id, exc)
        return delivered > 0

    def _validate_ai_order_against_catalog(
        self,
        ai_result: Dict,
        raw_text: str,
        products: List[Dict],
        contact_override: Optional[Dict] = None,
        customer_confirmed: tuple = (),
    ) -> Tuple[Optional[Dict], List[str], Dict]:
        """Canonicalize AI output and require a unique live catalog match.

        The model never chooses product IDs, prices, warehouses or stock. Every
        item is resolved again against the current backend catalog here.
        """
        errors: List[str] = []
        diagnostic_codes = []
        def diagnostic_error(code, message):
            diagnostic_codes.append(code)
            errors.append(message)
        # detail 让调用方能按「为什么失败」分流：口味不存在要回客户人话，
        # 售后单要转客服，其余才走原来的通用提示。
        detail: Dict = {'unresolved': [], 'is_aftersale': False}
        if not isinstance(ai_result, dict):
            diagnostic_reject(['INVALID_RESPONSE'])
            return None, ['DeepSeek 返回格式无效'], detail

        runtime = self._ai_runtime_config()
        from source_evidence import bind_reviewed_sources
        ai_result = bind_reviewed_sources(ai_result,raw_text,products)
        provider_address=str(ai_result.get('address') or '').strip()
        local_contact = parse_contact(raw_text, products=products)
        contact_conflicts=[field for field in FIELDS if local_contact.get(field) and ai_result.get(field)
                          and local_contact[field]!=ai_result[field]]
        if contact_conflicts:
            ai_result['_contact_source_conflicts']=contact_conflicts
        from contact_fields import with_generated_name
        from contact_fields import contact_catalog_names
        if (not local_contact.get('recipient')
                and str(ai_result.get('recipient') or '').casefold() in contact_catalog_names(products)):
            ai_result['recipient'] = None
        if not runtime['enabled'] or not runtime['contact_enabled']:
            ai_result.update({field: local_contact.get(field) for field in FIELDS})
        else:
            for field in FIELDS:
                if field not in local_contact and ai_result.get(field) and not self._contact_candidate_evidence(field, str(ai_result[field]), raw_text):
                    ai_result[field] = None
        ai_result = merge_contact(ai_result, local_contact)
        # A deterministic contact pass can occasionally keep a glued
        # ``model + flavor + quantity`` tail as part of the address.  Never
        # persist that pollution.  A provider address may replace it only
        # when the existing source guard proves that the provider preserved
        # the customer's explicit administrative nodes and contains no
        # catalog merchandise tokens.
        merged_address=str(ai_result.get('address') or '').strip()
        merchandise_tokens=[]
        for candidate in ai_result.get('items') or []:
            if not isinstance(candidate,dict):continue
            merchandise_tokens.extend(str(candidate.get(key) or '').strip()
                for key in ('raw_category','raw_flavor') if str(candidate.get(key) or '').strip())
        from address_source_guard import prefer_complete_source_address
        ai_result['address']=prefer_complete_source_address(
            raw_text,merged_address,provider_address,products,merchandise_tokens)
        from contact_fields import explicit_name_in_source, valid_name
        if not local_contact.get('recipient') and ai_result.get('recipient'):
            candidate_name=str(ai_result['recipient']).strip()
            if (valid_name(candidate_name) and self._contact_candidate_evidence('recipient',candidate_name,raw_text)
                    and not explicit_name_in_source(candidate_name,raw_text)):
                # A reasonable source-backed name/nickname no longer needs
                # an extra confirmation. AI still cannot invent a recipient.
                ai_result['_recipient_source']='SOURCE_AI'
        if contact_override is not None:
            if contact_override.get('_suspected_recipient') and not contact_override.get('recipient'):
                ai_result['recipient']=None
            ai_result = merge_contact(ai_result, contact_override)
        original_missing = [str(x) for x in ai_result.get('missing') or []]
        ai_result=with_generated_name(ai_result)
        # Customer warehouse names are free-form remarks, never routing input.
        # Actual fulfillment is selected from authorized inventory locally.
        warehouse_only_review=bool(original_missing) and set(original_missing)=={'warehouse'}
        original_missing=[x for x in original_missing if x!='warehouse']
        ai_result['warehouse']=None
        contact_status = diagnose_contact(ai_result)
        ai_result['missing'] = [x for x in original_missing if x not in FIELDS] + contact_status['missing']
        # The provider may flag manual review without listing a recipient it
        # supplied speculatively. Recomputed contact facts own the repair route;
        # independent catalog/source/quantity/business errors still block below.
        contact_only_review = all(x in FIELDS for x in original_missing) and bool(
            original_missing or contact_status['missing'] or contact_status['invalid'] or warehouse_only_review
        )
        detail['contact_ai'] = ai_result

        try:
            confidence = float(ai_result.get('confidence', 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        # Consent applies only to the selected item. Other items must already
        # have deterministic standard/reviewed-Alias evidence in the source.
        confirmed_indices={index for index in customer_confirmed if index < len(ai_result.get('items') or [])}
        for item_index, candidate in enumerate(ai_result.get('items') or []):
            if not isinstance(candidate, dict) or item_index in confirmed_indices:
                continue
            raw_category=str(candidate.get('raw_category') or candidate.get('category') or '')
            raw_flavor=str(candidate.get('raw_flavor') or candidate.get('flavor') or '')
            if raw_category and raw_flavor and raw_category in raw_text and raw_flavor in raw_text:
                from deepseek_resolver import resolve_category, resolve_identity_flavor
                categories=list(dict.fromkeys(p.get('parent_name') for p in products if p.get('parent_name')))
                category,_=resolve_category(raw_category,categories)
                pool=[p for p in products if p.get('parent_name')==category and p.get('is_active',True)]
                canonical,_=resolve_identity_flavor(category,raw_flavor,[p['name'] for p in pool],products=pool)
                if category and canonical:
                    confirmed_indices.add(item_index)
        fully_confirmed=bool(customer_confirmed) and bool(ai_result.get('items')) and confirmed_indices==set(range(len(ai_result['items'])))
        if confidence < float(DEEPSEEK_MIN_CONFIDENCE) and not fully_confirmed:
            diagnostic_error('CONFIDENCE_REJECTED', f'AI 置信度 {confidence:.2f} 低于安全阈值 {float(DEEPSEEK_MIN_CONFIDENCE):.2f}')

        if ai_result.get('needs_manual_review') and not contact_only_review and not fully_confirmed:
            diagnostic_error('BUSINESS_REJECTION', 'AI 标记该订单需要人工确认')

        # 缺货类要求（备选/随机/退单/精准）已经有自动执行逻辑，notes 只是给仓库看的
        # 说明，不再作为拦截依据；否则一条描述性备注就会堵死本来能自动下的单。
        # 售后 / 补发单涉及金额和责任归属，仍然挡下来单独处理。
        from ai_flavor_decision import normalize_repeat_selected
        ai_result=normalize_repeat_selected(ai_result,raw_text,products)
        directive = fulfillment_from_ai(ai_result)
        from ai_flavor_decision import fulfillment_evidence_errors
        for fulfillment_error in fulfillment_evidence_errors(ai_result,raw_text,products):
            diagnostic_error('SOURCE_EVIDENCE_FAILED',fulfillment_error)
        if directive.is_aftersale:
            detail['is_aftersale'] = True
            diagnostic_error('BUSINESS_REJECTION', '售后 / 补发单需要单独处理，未进入自动下单流程')

        missing = [str(item).strip() for item in (ai_result.get('missing') or []) if str(item).strip() and str(item).strip() not in FIELDS]
        if fully_confirmed:
            # A customer has resolved the flavor choice, not other missing
            # business fields. Catalog/source/quantity/contact checks below
            # still validate every selected item against current facts.
            missing = [field for field in missing if field not in ('flavor', 'product')]
        if missing:
            diagnostic_error('UNKNOWN_VALIDATION', 'AI 未能确定字段：' + '、'.join(missing[:6]))

        live_products = [p for p in (products or []) if p.get('name')]
        if not live_products:
            diagnostic_error('CATALOG_VALIDATION_FAILED', '后台商品目录为空，无法安全匹配')

        canonical_items = []
        seen_product_sources = {}
        for index, item in enumerate(ai_result.get('items') or [], start=1):
            if not isinstance(item, dict):
                diagnostic_error('INVALID_RESPONSE', f'第 {index} 个 AI 商品项格式无效')
                continue
            source_text = str(item.get('source_text') or '').strip()
            try:
                quantity = int(item.get('quantity'))
            except (TypeError, ValueError):
                quantity = 0
            if quantity <= 0:
                diagnostic_error('QUANTITY_INVALID', f'第 {index} 个 AI 商品缺少有效数量')
                continue
            if quantity > 100000:
                diagnostic_error('QUANTITY_INVALID', f'第 {index} 个 AI 商品数量异常：{quantity}')
                continue

            # 客户原话 -> 目录规范名，全程确定性规则，映射不出来就报错不猜。
            try:
                resolved = resolve_ai_item(item, live_products,
                    customer_confirmed=(index-1 in customer_confirmed))
            except ValueError as resolve_error:
                if 'Alias' in str(resolve_error) and '冲突' in str(resolve_error):
                    diagnostic_reject(['ALIAS_CONFLICT'])
                raise
            category = resolved.get('resolved_category')
            flavor = resolved.get('resolved_flavor')
            if not category or not flavor:
                raw_category = item.get('raw_category') or item.get('category')
                raw_flavor = item.get('raw_flavor') or item.get('flavor')
                # 品类认出来了只是口味对不上时，把该品类的可选口味一并带出去，
                # 好让机器人直接告诉客户「这个型号只有这些口味」。
                options = []
                if category:
                    seen_options = set()
                    for candidate in live_products:
                        if self._normalize_catalog_token(candidate.get('parent_name')) !=                                 self._normalize_catalog_token(category):
                            continue
                        name = str(candidate.get('name') or '').strip()
                        if name and name not in seen_options:
                            seen_options.add(name)
                            options.append(name)
                detail['unresolved'].append({
                    'raw_category': raw_category,
                    'raw_flavor': raw_flavor,
                    'quantity': quantity,
                    'category': category,
                    'options': options,
                    'reason': resolved.get('resolve_reason'),
                })
                resolve_reason = str(resolved.get('resolve_reason') or '')
                code = 'ALIAS_CONFLICT' if '冲突' in resolve_reason else ('CATEGORY_NOT_FOUND' if not category else 'PRODUCT_NOT_FOUND')
                diagnostic_error(code,
                    f'第 {index} 个商品无法映射到目录：{raw_category}/{raw_flavor}'
                    f'（{resolved.get("resolve_reason")}）'
                )
                continue

            category_token = self._normalize_catalog_token(category)
            flavor_token = self._normalize_catalog_token(flavor)
            matches = [
                p for p in live_products
                if self._normalize_catalog_token(p.get('parent_name')) == category_token
                and self._normalize_catalog_token(p.get('name')) == flavor_token
            ]
            if len(matches) == 0:
                diagnostic_error('PRODUCT_NOT_FOUND', f'AI 商品不在后台目录：品类({category})口味({flavor})')
                continue
            if len(matches) > 1:
                if self._one_business_product(matches) is None:
                    diagnostic_error('CATALOG_VALIDATION_FAILED', f'AI 商品匹配不唯一：品类({category})口味({flavor})')
                    continue
                matches=sorted(matches,key=lambda p:int(p.get('id') or 0))

            product = matches[0]
            product_id = product.get('id')
            from source_evidence import quantity_conflict
            if quantity_conflict(raw_text,item,live_products):
                diagnostic_error('QUANTITY_INVALID', f'第 {index} 个 AI 商品数量与客户明确口味数量不一致')
                continue
            global_flavor_matches = [
                p for p in live_products
                if self._normalize_catalog_token(p.get('name')) == flavor_token
            ]
            evidence_ok, evidence_reason = self._ai_item_evidence_ok(
                raw_text,
                item,
                require_category=len(global_flavor_matches) > 1,
                products=live_products,
            )
            if not evidence_ok:
                diagnostic_error('SOURCE_EVIDENCE_FAILED',
                    f'原文未找到 AI 商品依据：品类({product.get("parent_name")})'
                    f'口味({product.get("name")})（{evidence_reason}）'
                )
                continue
            # Same identity AND same proven source span is one candidate.
            # Different disjoint spans are legitimate repeated purchases;
            # never deduplicate solely by product_id/name or across warehouses.
            from source_evidence import actual_span
            actual=actual_span(raw_text,str(item.get('source_text') or source_text))
            occurrences=list(re.finditer(re.escape(actual),raw_text)) if actual else []
            span=occurrences[0].span() if len(occurrences)==1 else None
            previous=seen_product_sources.setdefault(product_id,[])
            if previous:
                if span is not None and (span,quantity) in previous:
                    continue
                if span is None or any(old is None or max(old[0],span[0])<min(old[1],span[1]) for old,_q in previous):
                    diagnostic_error('SOURCE_EVIDENCE_FAILED','同一商品的重复候选缺少独立原文位置，请确认整单')
                    continue
            previous.append((span,quantity))
            canonical_items.append({
                'category': str(product.get('parent_name') or '').strip() or None,
                'flavor': str(product.get('name') or '').strip(),
                'quantity': quantity,
                'source_text': str(item.get('source_text') or source_text),
                'raw_category': item.get('raw_category'),
                'raw_flavor': item.get('raw_flavor'),
                'resolve_reason': resolved.get('resolve_reason'),
            })

        if not canonical_items:
            diagnostic_error('CATALOG_VALIDATION_FAILED', 'AI 未识别到可安全匹配的商品')
        if errors:
            diagnostic_reject(diagnostic_codes)
            return None, errors, detail

        canonical_result = dict(ai_result)
        canonical_result['items'] = canonical_items
        order_info = to_order_info(canonical_result, raw_text)
        # Run the unchanged shared quantity-scope analysis on AI items too;
        # totals are never accepted just because the model returned them.
        order_info = OrderParser._attach_parse_metadata(raw_text, order_info, products)
        # Raw implicit-list markers are not extra merchandise. Every item
        # here already passed live catalog/source validation, including any
        # explicitly customer-confirmed flavor choice for this draft only.
        validated_raw_refs={'品类('+item['category']+')口味('+str(item.get('raw_flavor') or item['flavor'])+')'
                            for item in canonical_items}
        order_info['implicit_unresolved_items']=[ref for ref in order_info.get('implicit_unresolved_items',[])
                                                  if ref not in validated_raw_refs]
        order_info = merge_contact(order_info, ai_result)
        candidate_address=provider_address
        parsed_address=str(order_info.get('address') or '').strip()
        from address_source_guard import prefer_complete_source_address
        item_tokens=[str(item.get(key) or '') for item in canonical_items
                     for key in ('raw_category','raw_flavor')]
        order_info['address']=prefer_complete_source_address(
            raw_text,parsed_address,candidate_address,products,item_tokens)
        if order_info.get('quantity_scope_errors') or order_info.get('multiple_orders_detected') or order_info.get('requires_item_confirmation'):
            _valid, reason = OrderParser.validate_order(order_info)
            diagnostic_reject(['QUANTITY_INVALID'])
            return None, [reason], detail
        # Only set after catalog/source/quantity validation. Keep the original
        # scopes for diagnostics, but do not turn verified standard identities
        # back into the customer's not-yet-reviewed alias during preview.
        order_info['_validated_catalog_items'] = True
        if contact_status['missing'] or contact_status['invalid']:
            diagnostic_reject(['CONTACT_INVALID'])
            detail['contact_order'] = order_info
            detail['contact_status'] = contact_status
            return None, [contact_prompt(contact_status)], detail
        valid, reason = OrderParser.validate_order(order_info)
        if not valid:
            errors.append(reason)
            diagnostic_reject(['UNKNOWN_VALIDATION'])
            return None, errors, detail
        diagnostic_event('DETERMINISTIC_VALIDATION', 'SUCCESS')
        return order_info, [], detail

    def _infer_order_category(self, order_info: Dict, products: List[Dict]) -> Optional[str]:
        """从整条订单中先推断一个商品分类，供未写品类的口味使用。"""
        parent_names = []
        seen = set()
        for product in products:
            parent_name = str(product.get("parent_name") or "").strip()
            token = self._normalize_catalog_token(parent_name)
            if parent_name and token and token not in seen:
                seen.add(token)
                parent_names.append(parent_name)

        explicit_models = []
        for raw_ref, _quantity in order_info.get("items") or []:
            if isinstance(raw_ref, dict):
                raw_ref = raw_ref.get("product_name") or raw_ref.get("name") or ""
            model, _flavor = self._split_product_reference(str(raw_ref))
            if not model:
                continue
            # 同 _resolve_product_for_order：前缀匹配会把「板砖2W」算成一代
            model = self._canonical_catalog_category(model, products) or model
            matches = [
                parent_name
                for parent_name in parent_names
                if self._model_matches(parent_name, model)
            ]
            if len(matches) == 1 and matches[0] not in explicit_models:
                explicit_models.append(matches[0])
        if len(explicit_models) == 1:
            return explicit_models[0]
        if len(explicit_models) > 1:
            return None

        # No flavor-to-category inference or substring/generation guessing.
        return None


    def _catalog_category_candidates(
        self,
        order_info: Dict,
        products: List[Dict],
    ) -> List[str]:
        """按客户原文顺序找出确实存在于当前目录的分类候选。"""
        parent_names = []
        seen = set()
        for product in products:
            parent_name = str(product.get("parent_name") or "").strip()
            token = self._normalize_catalog_token(parent_name)
            if parent_name and token and token not in seen:
                seen.add(token)
                parent_names.append(parent_name)

        raw_text = str(order_info.get("raw_text") or "")
        if not raw_text or not parent_names:
            return []
        try:
            normalized = OrderParser._normalize_text(raw_text)
            lines = [line.strip() for line in normalized.splitlines() if line.strip()]
            if lines and re.fullmatch(r"(下单|购买|订单|下单模板)", lines[0].replace(" ", "")):
                lines = lines[1:]
            delivery_text = OrderParser._strip_embedded_items_for_delivery("\n".join(lines))
            delivery_lines = [line.strip() for line in delivery_text.splitlines() if line.strip()]
            address, recipient, phone, delivery_indices = OrderParser._extract_flexible_delivery(
                delivery_lines,
                delivery_text,
            )
            item_lines = OrderParser._extract_item_lines(
                lines,
                delivery_indices,
                address,
                recipient,
                phone,
            )
        except Exception:
            item_lines = raw_text.splitlines()

        candidates = []
        for line in item_lines:
            token = self._normalize_catalog_token(line)
            if not token:
                continue
            matches = [
                parent_name
                for parent_name in parent_names
                if self._normalize_catalog_token(parent_name) == token
            ]
            if len(matches) == 1 and matches[0] not in candidates:
                candidates.append(matches[0])
        return candidates

    def _resolve_order_category_context(
        self,
        order_info: Dict,
        products: List[Dict],
    ) -> Optional[str]:
        """仅使用实时目录可验证的品类；错误候选会回退到后续真实品类。"""
        category = self._infer_order_category(order_info, products)
        if category and any(
            self._model_matches(product.get("parent_name"), category)
            for product in products
        ):
            return category

        candidates = self._catalog_category_candidates(order_info, products)
        return candidates[0] if len(candidates)==1 else None

    def _explicit_catalog_reference(self, raw_ref, products):
        """Recover only explicit item evidence, before weak flavor normalization.

        This does not select a warehouse or change quantities. Conflicting or
        out-of-scope aliases remain references so the resolver can reject them.
        """
        from rule_dictionary import runtime_store
        text = str(raw_ref or '').strip()
        model, flavor = self._split_product_reference(text)
        aliases = {r['alias'] for r in runtime_store()['aliases']
                   if r['type'] == 'MODEL_ALIASES' and r['status'] == 'active'}
        names = aliases | set(self._live_parent_names(products))
        if model in names:
            return f'品类({model})口味({flavor})'
        prefixes = [n for n in names if text.startswith(n + ' ')]
        if prefixes:
            prefix = max(prefixes, key=len)
            return f'品类({prefix})口味({text[len(prefix):].strip()})'
        # Explicit labels have field boundaries, not arbitrary substring hits.
        match = re.fullmatch(r'(?:品牌|分类|品类|型号)[:：]\s*(.+?)\s+(?:口味[:：]\s*)?(.+)', text)
        if match and match.group(1) in names:
            return f'品类({match.group(1)})口味({match.group(2)})'
        return None

    def _order_catalog_items(self, order_info, products):
        evidence = [s for s in order_info.get('quantity_scopes', [])
                    if s.get('kind') == 'ITEM_QUANTITY']
        items = order_info.get('items') or []
        if order_info.get('_validated_catalog_items'):
            return list(items)
        result = []
        for index, (reference, quantity) in enumerate(items):
            original_reference = self._explicit_catalog_reference(reference, products)
            if original_reference:
                # Scope facts may use shortened tokenizer labels. They can
                # supply category context, never replace an already scoped
                # flavor with a shorter, different flavor.
                result.append((original_reference, quantity))
                continue
            if len(evidence) == len(items) and evidence[index].get('quantity') == quantity:
                fact = evidence[index]
                explicit = self._explicit_catalog_reference(fact.get('item'), products)
                if not explicit and fact.get('section'):
                    # The shared parser already assigned this quantity to a
                    # section. Preserve that per-item scope through catalog
                    # matching, including multiple brands in one order.
                    scoped = '品类(' + str(fact['section']) + ')口味(' + str(reference) + ')'
                    explicit = self._explicit_catalog_reference(scoped, products)
                reference = explicit or reference
            result.append((reference, quantity))
        return result

    @staticmethod
    def _one_business_product(candidates):
        """Warehouse copies alone are not different business products."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        identities = {(p.get('parent_name'), p.get('name'),
                       p.get('model'), p.get('generation')) for p in candidates}
        warehouses = {p.get('default_warehouse_id') for p in candidates}
        if len(identities) == 1 and None not in warehouses and len(warehouses) == len(candidates):
            return min(candidates, key=lambda p: int(p.get('id') or 0))
        return None

    @__import__('order_trace').timed_sync('CATALOG_MATCH')
    def _resolve_product_for_order(
        self,
        raw_ref,
        products: List[Dict],
        category_context: Optional[str] = None,
    ) -> Tuple[Optional[Dict], Optional[str]]:
        if isinstance(raw_ref, dict):
            product_id = raw_ref.get('product_id')
            if product_id:
                product = next((p for p in products if int(p.get('id') or 0) == int(product_id)), None)
                return product, None if product else f"商品 ID {product_id} 不存在"
            raw_ref = raw_ref.get('product_name') or raw_ref.get('name') or ''
        model, flavor = self._split_product_reference(str(raw_ref))
        model = model or category_context
        from brand_aliases import scope_catalog
        scoped = scope_catalog(model, products)
        if scoped is not None:
            if not scoped:
                return None, '品牌叫法在当前仓库/分类范围内不能唯一匹配'
            products = scoped
        # 「板砖2W」这类写法必须先规范化成目录品类名，否则前缀匹配会落到旧代上。
        canonical_model=self._canonical_catalog_category(model, products)
        if not canonical_model:
            return None,'商品分类未命中后台标准或已审核叫法，请填写准确分类；不猜测分类'
        if (model and canonical_model and re.search(r'[一二三四五六七八九\d]+代',str(model))
                and not re.search(r'[一二三四五六七八九\d]+代',canonical_model)
                and scoped is None):
            return None,f'品类代次需要确认：{model}，当前目录没有可核对的对应代次'
        model = canonical_model or model
        scoped_products = [
            p for p in products
            if not model or self._model_matches(p.get('parent_name'), model)
        ]
        exact_category=[p for p in products if model and
                        self._normalize_catalog_token(p.get('parent_name'))==self._normalize_catalog_token(model)]
        if exact_category:
            scoped_products=exact_category
        if model:
            from rule_dictionary import manual_flavor
            try:
                flavor=manual_flavor(model,flavor,[p.get('name') for p in scoped_products],scoped_products) or flavor
            except ValueError as error:
                return None,str(error)
        exact = [
            p for p in scoped_products
            if self._normalize_catalog_token(p.get('name')) == self._normalize_catalog_token(flavor)
        ]
        resolved = self._one_business_product(exact)
        if resolved:
            return resolved, None
        if len(exact) > 1:
            if model:
                return None, f"口味 '{flavor}' 在品类 '{model}' 下存在多个商品，请确认具体型号"
            categories = sorted({
                str(p.get("parent_name") or "未分类").strip()
                for p in exact
            })
            category_hint = "、".join(categories[:5])
            return None, f"口味 '{flavor}' 匹配到多个品类（{category_hint}），请补充商品分类"
        return None, f"商品不存在：品类({model})口味({flavor})；请确认标准口味后重新发送完整订单"

    def _parse_inbound_item(self, part: str) -> Optional[Dict]:
        """解析单条入库项，支持: 苹果 +50 / 苹果=100 / 苹果 x50 / 苹果50个"""
        part = (part or "").strip()
        if not part:
            return None

        patterns = [
            r'^(?P<name>.+?)\s*(?P<mode>[+=])\s*(?P<qty>\d+)\s*个?$',
            r'^(?P<name>.+?)\s*[xX×*]\s*(?P<qty>\d+)\s*个?$',
            r'^(?P<name>.+?)\s+(?P<qty>\d+)\s*个?$',
            r'^(?P<name>.+?)(?P<qty>\d+)\s*个$',
        ]
        for pattern in patterns:
            m = re.match(pattern, part)
            if not m:
                continue
            name = m.group('name').strip()
            qty = int(m.group('qty'))
            if not name or qty < 0:
                continue
            mode = 'set' if m.groupdict().get('mode') == '=' else 'add'
            return {'product_name': name, 'quantity': qty, 'mode': mode}
        return None

    def _parse_inbound_text(self, text: str) -> Tuple[Optional[str], List[Dict], Optional[str]]:
        """解析入库文本。

        返回: (warehouse_name, items[{product_name, quantity, mode}], error)
        mode: add/set
        """
        raw = (text or "").strip()
        if not raw:
            return None, [], "内容为空"

        # 去掉命令前缀
        raw = re.sub(r'^/inbound(@\w+)?\s*', '', raw, flags=re.I).strip()
        raw = re.sub(r'^(入库|到货入库|补货)[:：\s]*', '', raw).strip()

        warehouse_name = None
        # 仅匹配显式标签，避免误伤“北京仓库”中的“仓/仓库”
        wh_match = re.search(r'(?:^|[\n,，\s])(?:仓库|仓)[:：]\s*([^\n,，]+)', raw)
        if not wh_match:
            wh_match = re.search(r'(?:仓库|仓)[:：]\s*([^\n,，]+)', raw)
        if wh_match:
            warehouse_name = wh_match.group(1).strip()
            raw = (raw[:wh_match.start()] + raw[wh_match.end():]).strip(" ,，\n")

        # 单行命令: 苹果 50 北京仓库 / 苹果 +50 / 苹果 =100 默认仓库
        if (',' not in raw and '，' not in raw and '\n' not in raw):
            tokens = raw.split()
            if len(tokens) >= 2:
                # 末尾若不是纯数字，优先当作仓库名
                maybe_wh = tokens[-1]
                body_tokens = tokens
                if not re.fullmatch(r'[+=]?\d+', maybe_wh) and not re.search(r'\d', maybe_wh[-1:]):
                    # 最后 token 含中文仓库名的概率高
                    if not re.fullmatch(r'.+[+=]?\d+', maybe_wh):
                        warehouse_name = warehouse_name or maybe_wh
                        body_tokens = tokens[:-1]
                body = ' '.join(body_tokens)
                item = self._parse_inbound_item(body)
                if item:
                    return warehouse_name, [item], None

        # 列表式: 苹果 +50, 香蕉 20, 橙子=30
        parts = re.split(r'[,，\n]+', raw)
        items = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            item = self._parse_inbound_item(part)
            if not item:
                return warehouse_name, [], f"无法解析: {part}"
            items.append(item)

        if not items:
            return warehouse_name, [], "未识别到入库商品"
        return warehouse_name, items, None

    async def inbound(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/inbound 到货入库"""
        user_id = update.effective_user.id
        if not self._is_admin(user_id):
            await update.message.reply_text("❌ 您没有权限执行入库操作")
            return

        text = update.message.text or ""
        # 如果只输入命令，给出帮助
        body = re.sub(r'^/inbound(@\w+)?\s*', '', text, flags=re.I).strip()
        if not body:
            await update.message.reply_text(INBOUND_FORMAT_EXAMPLE, parse_mode='HTML')
            return

        await self._process_inbound(update, text)

    async def _process_inbound(self, update: Update, text: str):
        warehouse_name, items, error = self._parse_inbound_text(text)
        if error:
            await update.message.reply_text(f"❌ 入库解析失败: {error}\n\n{INBOUND_FORMAT_EXAMPLE}", parse_mode='HTML')
            return

        # 入库是管理员操作，允许管理员明确选择任一仓库；这里不走客户可见性过滤。
        products = (await self._api_call('get_products')) or []
        warehouses = (await self._api_call('get_all_warehouses')) or []
        warehouse = self._match_warehouse(warehouse_name, warehouses)
        if not warehouse:
            await update.message.reply_text("❌ 未找到可用仓库，请先在后台创建仓库")
            return

        success_lines = []
        fail_lines = []
        for item in items:
            product = self._match_product(item['product_name'], products)
            if not product:
                fail_lines.append(f"❌ {item['product_name']}: 商品不存在")
                continue
            ok, resp = (await self._api_call('inbound_stock', 
                product_id=product['id'],
                quantity=item['quantity'],
                warehouse_id=warehouse['id'],
                mode=item.get('mode', 'add')
            ))
            if ok and isinstance(resp, dict):
                mode_text = "覆盖" if resp.get('mode') == 'set' else "入库"
                success_lines.append(
                    f"✅ {resp.get('product_name')} @ {resp.get('warehouse_name')}: "
                    f"{resp.get('old_quantity')} → {resp.get('new_quantity')} ({mode_text} {item['quantity']})"
                )
            else:
                fail_lines.append(f"❌ {item['product_name']}: {resp}")

        message = f"📥 <b>到货入库结果</b>\n仓库: {warehouse.get('name')}\n\n"
        if success_lines:
            message += "\n".join(success_lines) + "\n"
        if fail_lines:
            message += "\n" + "\n".join(fail_lines)
        if not success_lines and not fail_lines:
            message += "没有可处理的条目"
        await update.message.reply_text(message, parse_mode='HTML')

    @classmethod
    def _sales_section(cls) -> Dict:
        """按仓库停仓的开关。每次现读 config —— 后台可能刚改过，Bot 不重启。"""
        import config as cfg
        return getattr(cfg, 'SALES_PAUSE_SECTION', None) or {}

    @classmethod
    def _sales_paused(cls) -> bool:
        """是否所有仓库都停了（等价于原来的「一键停仓」）。

        config.SALES_PAUSED 现在是后台按仓库开关算出来的派生值：全部停才为真。
        """
        import config as cfg
        return bool(getattr(cfg, 'SALES_PAUSED', False))

    @classmethod
    def _sales_paused_text(cls, warehouse_id=None) -> str:
        """给客户看的停仓提示语：该仓库自己的 > 全局的 > 默认。"""
        import config as cfg
        return sales_pause.message_for(
            cls._sales_section(),
            warehouse_id,
            str(getattr(cfg, 'SALES_PAUSED_MESSAGE', '') or ''),
        )

    @classmethod
    def _warehouse_paused(cls, warehouse) -> bool:
        return sales_pause.is_paused(cls._sales_section(), (warehouse or {}).get('id'))

    async def _bundle_candidate(self,text):
        """Exact independent Bundle names also qualify without category/contact."""
        try:
            from bundle_text import extract
            catalog=await self._api_call('get_bundle_catalog')
            return isinstance(catalog,list) and bool(extract(text,catalog)[0])
        except (ValueError,TypeError,KeyError):
            return False
        except Exception:
            return False

    async def _handle_bundle_text(self,update,context,raw_text):
        from bundle_text import extract
        try:
            catalog=await self._api_call('get_bundle_catalog')
        except Exception:
            return False
        if not isinstance(catalog,list) or not catalog:return False
        try:
            requests,remainder=extract(raw_text,catalog)
            if not requests:return False
            products,_,_=await self._visible_catalog_for_user_async(update)
            info=OrderParser.extract_order_info(remainder,products) or {}
            # A Bundle sale is not permission to bypass ordinary quantity or
            # contact evidence safety in a mixed order.
            if info.get('multiple_orders_detected'):
                raise ValueError('当前消息含多个收件手机号，请按收件地址分别发送')
            if info.get('quantity_scope_errors'):
                raise ValueError('普通商品数量需要确认：'+'；'.join(info['quantity_scope_errors']))
            if info.get('requires_item_confirmation'):
                raise ValueError('普通商品配货说明需要确认，请填写明确口味和数量')
            ordinary=[]
            for name,qty in self._order_catalog_items(info,products):
                p,error=self._resolve_product_for_order(name,products)
                if not p:raise ValueError(error or '普通商品无法确认')
                ordinary.append(dict(product_id=p['id'],quantity=qty))
            for request in requests:
                bundle=next(b for b in catalog if b['id']==request['bundle_id'])
                if not bundle.get('sellable',True):
                    raise ValueError('福袋「'+bundle['name']+'」暂不可售：'+(bundle.get('error') or '已停用'))
                if bundle['config']['mode']=='custom':
                    choices=[]
                    text='品类：'+bundle['category_name']+'\n'+'\n'.join(request.pop('selection_text'))
                    chosen=OrderParser.extract_order_info(text,bundle['flavors']) or {}
                    if chosen.get('quantity_scope_errors') or chosen.get('requires_item_confirmation'):
                        raise ValueError('福袋自选口味数量需要确认，请填写明确口味和数量')
                    for name,qty in self._order_catalog_items(chosen,bundle['flavors']):
                        p,error=self._resolve_product_for_order(name,bundle['flavors'],category_context=bundle['category_name'])
                        if not p:raise ValueError(error or '福袋自选口味无法确认')
                        choices.append(dict(product_id=p['id'],quantity=qty))
                    # One selection represents the per-portion composition; it
                    # must satisfy the configured size for every bought portion.
                    request['selections']={str(i):choices for i in range(request['quantity'])}
                else:request.pop('selection_text',None)
            fields=await self._contact_fields_async(remainder,info)
            info=merge_contact(info,fields)
            info.update(bundle_requests=requests,bundle_ordinary_items=ordinary,raw_text=raw_text)
            if fields.get('_name_absent_confirmed'):info['_name_absent_confirmed']=True
            status=diagnose_contact(info)
            if status['missing'] or status['invalid']:
                context.user_data['pending_recipient_order']=dict(order_info=info,raw_text=raw_text)
                await self._contact_problem_reply(update,context,info,status)
                return True
            preview=await self._build_order_preview(update,info)
            if preview:await self._send_order_preview(update,context,preview)
            return True
        except ValueError as error:
            await update.effective_message.reply_text('❌ '+str(error))
            return True

    @__import__('order_trace').timed_async('ORDER_PREVIEW')
    async def _build_order_preview(self, update: Update, order_info: Dict) -> Optional[Dict]:
        """构建下单预览（匹配仓库/校验库存），不真正创建订单"""
        if getattr(update, 'message', None) is None and getattr(update, 'effective_message', None) is not None:
            original_update = update
            class CallbackOrderUpdate:
                message = original_update.effective_message
                def __getattr__(self, name):
                    return getattr(original_update, name)
            update = CallbackOrderUpdate()
        # 后台可在 Bot 运行期间修改运费，预览前刷新共享运行时配置。
        try:
            from config import load_runtime_settings
            load_runtime_settings()
        except Exception:
            pass
        import config as cfg_pkg

        # 停仓：所有下单路径（AI 识别、手动格式、购物车、快速下单）都会走到这里，
        # 一处拦截即可全覆盖。只挡新订单，已生成订单的发货/打单/售后不受影响。
        # 这里拦的是「全部仓库都停」；只停一部分时走下面的选仓剔除。
        if self._sales_paused():
            await update.message.reply_text(self._sales_paused_text())
            return None

        telegram_id = str(update.effective_user.id)
        success, user = await self._api_call(
            'create_or_get_user',
            telegram_id,
            update.effective_user.username,
            getattr(update.effective_user, 'full_name', None),
        )
        if not success:
            await update.message.reply_text("❌ 无法获取用户信息，请稍后重试")
            return None

        # 异常地址拦截（下单预览阶段）
        addr = order_info.get("address") or order_info.get("recipient_address") or ""
        try:
            ok, check = await self._api_call(
                '_make_request',
                "POST",
                "/api/blocked_addresses/check",
                data={"address_text": addr},
            )
            if ok and isinstance(check, dict) and check.get("blocked"):
                matched = (check.get("matched") or {}).get("address_text") or addr
                reason = (check.get('matched') or {}).get('reason') or '地址异常'
                await update.message.reply_text(
                    f'❌ 下单失败\n当前地址「{matched}」因「{reason}」不接单。')
                return None
        except Exception:
            pass

        if order_info.get('bundle_requests'):
            ok,result=await self._api_call('preview_bundles',telegram_id,
                order_info['bundle_requests'],order_info.get('bundle_ordinary_items') or [])
            if not ok or not isinstance(result,dict):
                await update.message.reply_text('❌ '+(result if isinstance(result,str) else '福袋配置或库存暂不可用'))
                return None
            result.update(telegram_id=telegram_id,user=user,
                recipient_name=order_info.get('recipient'),recipient_phone=order_info.get('phone'),
                recipient_address=order_info.get('address'),raw_text=order_info.get('raw_text'),
                source_chat_id=str(update.effective_chat.id),
                source_message_id=getattr(update,'_rainbow_source_message_id',update.effective_message.message_id),
                source_chat_type=update.effective_chat.type,
                source_chat_title=getattr(update.effective_chat,'title',None))
            return result

        warehouses = self._visible_warehouses(
            await self._api_call('get_all_warehouses'), update=update, user=user,
        )
        if not warehouses:
            await update.message.reply_text("❌ 系统未配置仓库，请联系管理员")
            return None

        # 按仓库停仓：被停的仓库直接从选仓流程里剔除，默认仓被停会自动落到其他仓。
        _sales_section = self._sales_section()
        open_warehouses = sales_pause.active_warehouses(_sales_section, warehouses)
        paused_warehouses = sales_pause.paused_warehouses(_sales_section, warehouses)
        if not open_warehouses:
            await update.message.reply_text(
                self._sales_paused_text(
                    paused_warehouses[0].get('id') if paused_warehouses else None
                )
            )
            return None

        products = self._visible_products(await self._api_call('get_products'), warehouses)
        product_dict = {p['id']: p for p in products}

        # customer_warehouse_notes are intentionally ignored, not mapped to a
        # backend warehouse and never a reason to reject an otherwise valid order.

        matched_items = []
        errors = []
        total_product_amount = 0
        warehouse_usage = {}
        total_stock_map = await self._product_stock_map_async(warehouses, products)
        category_context = self._resolve_order_category_context(order_info, products)

        unresolved = order_info.get('implicit_unresolved_items') or []
        if unresolved:
            retained = [(ref, qty) for ref, qty in order_info.get('items', []) if ref not in unresolved]
            message = '⚠️ 以下口味需要确认（未写数量默认每个1支）：\n' + '\n'.join(
                '• '+str(ref)+' ×1' for ref in unresolved)
            if retained:
                message += '\n\n其余识别明细：\n' + '\n'.join(
                    '• '+str(ref)+' ×'+str(qty) for ref,qty in retained)
            message += '\n\n请补充待确认口味的标准名称后重新发送完整订单；本次未录单、未扣库存。'
            await update.message.reply_text(message)
            return None

        # Explicit quantities need the same confirmation gate as implicit lists.
        # A spelling suggestion is never permission to substitute a product.
        catalog_confirmations=[]
        catalog_retained=[]
        for reference,quantity in self._order_catalog_items(order_info,products):
            product,error=self._resolve_product_for_order(reference,products,category_context=category_context)
            if product:
                catalog_retained.append(f"• {product.get('parent_name') or ''} / {product['name']} ×{quantity}")
            else:
                model,flavor=self._split_product_reference(str(reference))
                from difflib import get_close_matches
                candidates=sorted({str(p.get('name') or '') for p in products
                                   if model and self._model_matches(p.get('parent_name'),model)})
                suggestions=get_close_matches(flavor,candidates,n=2,cutoff=0.65)
                hint=('；是否指：'+'、'.join(suggestions)+'？（未自动替换）') if suggestions else ''
                catalog_confirmations.append(f'• {reference} ×{quantity}：{error or "无法唯一匹配"}{hint}')
        if catalog_confirmations:
            message='⚠️ 以下商品需要确认：\n'+'\n'.join(catalog_confirmations)
            if catalog_retained:
                message+='\n\n已识别明细（尚未录单）：\n'+'\n'.join(catalog_retained)
            message+='\n\n请确认待识别口味的标准名称后重新发送完整订单；本次未录单、未扣库存。'
            await update.message.reply_text(message)
            return None

        # Bind original primary quantities to standard product IDs before any
        # warehouse allocation or customer-authorized backup substitution.
        from product_order_limits import violations, rejection
        primary=[]
        for reference,quantity in self._order_catalog_items(order_info,products):
            product,_=self._resolve_product_for_order(reference,products,category_context=category_context)
            if product:primary.append(dict(product_id=product['id'],quantity=quantity))
        exceeded=violations(primary,products)
        if exceeded:
            order_info['_product_limit_failed']=True
            await update.message.reply_text(rejection(exceeded))
            return None

        # 客户写了缺货怎么办（备选/随机/退单/精准）时，先按策略把清单排一遍，再进入
        # 下面原有的选仓流程。放在这里而不是改选仓循环，是为了不影响批发和本地解析
        # 两条不带 AI 指令的路径。
        fulfillment_actions: List[str] = []
        if not order_info.get('ai_fulfillment'):
            try:
                from local_fulfillment import attach
                order_info=attach(order_info,products,
                    lambda name:self._resolve_product_for_order(name,products,category_context=category_context),
                    stock_of=lambda pid:int(total_stock_map.get(pid,0) or 0),
                    is_available=lambda p,needed: (
                        p.get('default_warehouse_id') in {w['id'] for w in open_warehouses}
                        and not is_stock_paused(int(total_stock_map.get(p['id'],0) or 0),
                                               int(p.get('low_stock_threshold') or 0))
                        and int(total_stock_map.get(p['id'],0) or 0)>=needed))
            except ValueError as exc:
                await update.effective_message.reply_text(str(exc))
                return None
        directive = fulfillment.from_order_info(order_info)
        structured_items = order_info.get('ai_items') or []
        if directive.is_automatic and structured_items:
            planned, fulfillment_actions, plan_errors = fulfillment.plan_items(
                structured_items,
                directive,
                [p for p in products if p.get('default_warehouse_id') in {w['id'] for w in open_warehouses}],
                stock_of=lambda pid: int(total_stock_map.get(pid, 0) or 0),
                normalize=self._normalize_catalog_token,
                # 停售判断复用同一个函数，否则 STOCK_AUTO_UNLIST_MODE 一改，
                # 缺货策略和真实下单闸门就会不一致，导致有货也替换成备选。
                is_paused=is_stock_paused,
            )
            if plan_errors:
                await update.message.reply_text(
                    '⚠️ 按客户的缺货要求处理后无法下单:\n\n'
                    + '\n'.join(f'• {reason}' for reason in plan_errors[:6])
                )
                return None
            order_info = dict(order_info)
            order_info['items'] = [
                (
                    f'品类({item.get("category")})口味({item.get("flavor")})'
                    if item.get('category') else str(item.get('flavor')),
                    int(item.get('quantity') or 0),
                )
                for item in planned
            ]
            # The planner has already resolved customer-authorized choices
            # against this catalog. Original quantity evidence must not turn
            # a selected backup back into the out-of-stock preferred item.
            order_info['_validated_catalog_items'] = True
            if fulfillment_actions:
                logger.info('缺货策略已应用: %s', '; '.join(fulfillment_actions))

        user_prices = await self._api_call('get_user_prices', user['id']) or {}
        for raw_product_ref, quantity in self._order_catalog_items(order_info, products):
            if raw_product_ref in order_info.get('implicit_unresolved_items', []):
                errors.append(f"口味需要确认：{raw_product_ref} ×{quantity}（未写数量默认1支）；请补充该口味的标准名称，其他商品已保留。")
                continue
            # Shared Parser quantity facts retain the original item token even
            # when its legacy flavor normalization drops a scoped brand alias.
            # Only one-to-one, quantity-aligned evidence can refine the ref.
            product, match_error = self._resolve_product_for_order(
                raw_product_ref,
                products,
                category_context=category_context,
            )
            if not product:
                errors.append(match_error or f"商品 '{raw_product_ref}' 不存在")
                continue
            # Warehouse copies of an already-resolved exact logical item do
            # not introduce a new flavor choice. Select an available copy for
            # preview; authoritative daily allocation remains in the API.
            from brand_aliases import scope_catalog
            reference_model, _reference_flavor = self._split_product_reference(str(raw_product_ref))
            reference_scope = scope_catalog(reference_model, products)
            variant_catalog = reference_scope if reference_scope is not None else products
            variants=[p for p in variant_catalog if p.get('name')==product.get('name')
                and p.get('parent_name')==product.get('parent_name')
                and p.get('model')==product.get('model')
                and p.get('generation')==product.get('generation')
                and p.get('default_warehouse_id') in {w['id'] for w in open_warehouses}
                and p.get('is_active',True)
                and int(total_stock_map.get(p['id'],0))>=quantity
                and not is_stock_paused(int(total_stock_map.get(p['id'],0)),normalize_threshold(p.get('low_stock_threshold')))]
            if variants:
                product=max(variants,key=lambda p:(int(total_stock_map.get(p['id'],0)),-int(p['id'])))

            product_id = product['id']
            product_name = product['name']
            parent_name = product.get('parent_name') or ''
            display_name = f"品类({parent_name})口味({product_name})" if parent_name else product_name
            default_warehouse_id = product.get('default_warehouse_id')
            selected_warehouse = None
            current_stock = 0
            total_stock = int(total_stock_map.get(product_id, 0) or 0)
            threshold = normalize_threshold(product.get('low_stock_threshold'))
            if is_stock_paused(total_stock, threshold):
                errors.append(
                    f"❌ 商品 '{display_name}' TG机器人暂不出货\n"
                    f"{stock_pause_text(total_stock, threshold)}"
                )
                continue

            if default_warehouse_id:
                # 停仓的仓库不在 open_warehouses 里，默认仓被停时自动落到下面的通选
                default_warehouse = next((w for w in open_warehouses if w['id'] == default_warehouse_id and w.get('is_active', True)), None)
                if default_warehouse:
                    stock_ok, error_msg = await self._api_call(
                        'check_warehouse_stock',
                        warehouse_id=default_warehouse_id,
                        product_id=product_id,
                        required_quantity=quantity
                    )
                    if stock_ok:
                        selected_warehouse = default_warehouse
                        stock_info = await self._api_call(
                            'get_warehouse_stock', default_warehouse_id, product_id
                        )
                        current_stock = stock_info.get('quantity', 0) if stock_info else 0
                    else:
                        errors.append(
                            f"❌ 商品 '{display_name}' 在默认仓库 ({default_warehouse['name']}) 库存不足\n"
                            f"需要数量: {quantity}个\n{error_msg}"
                        )
                        continue

            if not selected_warehouse:
                # 商品只能从 default_warehouse_id 对应的绑定仓发货，不能跨仓兜底。
                errors.append(
                    f"商品 '{display_name}' 在绑定仓库库存不足或仓库不可用，禁止跨仓发货"
                )
                continue

            if False and not selected_warehouse:
                available_warehouses = []
                for warehouse in open_warehouses:
                    if not warehouse.get('is_active', True):
                        continue
                    stock_ok, _ = await self._api_call(
                        'check_warehouse_stock',
                        warehouse_id=warehouse['id'],
                        product_id=product_id,
                        required_quantity=quantity
                    )
                    if stock_ok:
                        stock_info = await self._api_call(
                            'get_warehouse_stock', warehouse['id'], product_id
                        )
                        if stock_info:
                            available_warehouses.append({
                                'warehouse': warehouse,
                                'stock': stock_info.get('quantity', 0)
                            })
                if not available_warehouses:
                    hint = (
                        f"❌ 商品 '{display_name}' 库存不足\n需要数量: {quantity}个\n提示: 该商品在所有仓库的库存都不足"
                    )
                    # 有仓库正在停仓时必须说出来，否则「后台明明有货」会被当成 bug
                    if paused_warehouses:
                        _paused_names = '、'.join(
                            str(w.get('name') or '') for w in paused_warehouses
                        )
                        hint += f"\n（{_paused_names} 已停仓，不参与发货）"
                    errors.append(hint)
                    continue
                best = max(available_warehouses, key=lambda x: x['stock'])
                selected_warehouse = best['warehouse']
                current_stock = best['stock']

            unit_price = self._product_price_for_user(product, user, user_prices)
            total_product_amount += unit_price * quantity
            warehouse_usage[selected_warehouse['id']] = selected_warehouse
            matched_items.append({
                'product_id': product_id,
                'product_name': product_name,
                'parent_name': parent_name,
                'parent_id': product.get('parent_id'),
                'display_name': display_name,
                'quantity': quantity,
                'unit_price': unit_price,
                'unit': product.get('unit','支'),
                'warehouse_id': selected_warehouse['id'],
                'warehouse_name': selected_warehouse['name'],
                'shipping_fee': selected_warehouse.get('shipping_fee', 0) or 0,
                'current_stock': current_stock,
            })

        if errors:
            if not self._is_agent_group(update):
                await update.message.reply_text("⚠️ 订单校验失败:\n\n" + "\n\n".join(errors))
                return None
            failed_preview = {
                'items': [dict(ref,quantity=qty) if isinstance(ref,dict) else {'display_name':str(ref),'quantity':qty}
                          for ref,qty in (order_info.get('items') or [])],
                'recipient_name': order_info.get('recipient') or order_info.get('recipient_name'),
                'recipient_phone': order_info.get('phone') or order_info.get('recipient_phone'),
                'recipient_address': order_info.get('address') or order_info.get('recipient_address'),
                'total_amount': 0,
                'grand_total': 0,
            }
            await self._reply_agent_group_message(
                update,
                self._agent_group_order_text(failed_preview, error='; '.join(str(item) for item in errors[:8])),
                parse_mode='HTML',
            )
            return None
        if not matched_items:
            if self._is_agent_group(update):
                await self._send_agent_group_failure(
                    update, order_info, [], '没有识别到可下单的商品。'
                )
                return None
            await update.message.reply_text("❌ 没有有效的商品可以下单")
            return None

        # 数量 / 包裹闸门。放这里而不是更早：缺货策略（备选/随机）会改数量，
        # 必须拿最终清单判。权威检查在 api 的创建订单接口，这里是为了让客户在
        # 预览阶段就看到人话，而不是提交后收到一个 400。
        packaging_limits = package_rules.normalize({
            'max_units_per_order': getattr(cfg_pkg, 'MAX_UNITS_PER_ORDER', 0),
            'units_per_package': getattr(cfg_pkg, 'UNITS_PER_PACKAGE', 0),
            'address_daily_max_units': getattr(cfg_pkg, 'ADDRESS_DAILY_MAX_UNITS', 0),
        })
        total_units = package_rules.total_units(matched_items)
        # The configured maximum is a formal-suborder capacity, not a reason
        # to reject an otherwise valid multi-package order.
        allowed, reason = package_rules.check_units(total_units, dict(packaging_limits,max_units_per_order=0))
        if not allowed:
            if self._is_agent_group(update):
                await self._send_agent_group_failure(update, order_info, matched_items, reason)
                return None
            await update.message.reply_text(reason)
            return None
        # Address limits enqueue inside the authoritative creation transaction.


        # 运费：默认数量规则（普通5只/6元，烟弹10颗/6元）
        from warehouse_shipping import partition as partition_shipping
        scoped_shipping_fee, shipping_items = partition_shipping(matched_items)
        shipping_warehouses = {it['warehouse_id'] for it in shipping_items}
        try:
            from config import (
                SHIPPING_MODE, SHIPPING_FEE_AMOUNT, SHIPPING_NORMAL_QTY,
                SHIPPING_POD_QTY, SHIPPING_POD_KEYWORDS,
                SHIPPING_NORMAL_FEE_AMOUNT, SHIPPING_POD_FEE_AMOUNT,
            )
            import math
            if SHIPPING_MODE != 'warehouse':
                pod_qty = 0
                normal_qty = 0
                for it in shipping_items:
                    name = str(it.get('product_name') or '')
                    parent_name = str(it.get('parent_name') or '')
                    display_name = str(it.get('display_name') or '')
                    qty = int(it.get('quantity') or 0)
                    blob = f"{parent_name} {name} {display_name}".lower()
                    is_pod = any((kw or '').lower() in blob for kw in SHIPPING_POD_KEYWORDS)
                    if is_pod:
                        pod_qty += qty
                    else:
                        normal_qty += qty
                normal_fee_amount = float(SHIPPING_NORMAL_FEE_AMOUNT)
                pod_fee_amount = float(SHIPPING_POD_FEE_AMOUNT)
                if normal_qty > 0:
                    normal_segments = int(math.ceil(normal_qty / float(SHIPPING_NORMAL_QTY)))
                else:
                    normal_segments = 0
                if pod_qty > 0:
                    pod_segments = int(math.ceil(pod_qty / float(SHIPPING_POD_QTY)))
                else:
                    pod_segments = 0
                total_shipping_fee = (
                    normal_segments * normal_fee_amount
                    + pod_segments * pod_fee_amount
                )
            else:
                total_shipping_fee = sum(w.get('shipping_fee', 0) or 0 for wid, w in warehouse_usage.items() if wid in shipping_warehouses)
        except Exception:
            total_shipping_fee = sum(w.get('shipping_fee', 0) or 0 for wid, w in warehouse_usage.items() if wid in shipping_warehouses)
        total_shipping_fee += scoped_shipping_fee
        if SHIPPING_MODE!='warehouse':
            from parcel_shipping import total as parcel_total
            catalog_by_id={p['id']:p for p in products}
            total_shipping_fee=parcel_total([dict(it,product=catalog_by_id[it['product_id']]) for it in matched_items])
        # Quote owns the single final warehouse's fee, including category
        # overrides/default fallback. Do not render the stale catalog fee.
        final_warehouses = {it['warehouse_id'] for it in matched_items}
        if len(final_warehouses) == 1:
            for index, item in enumerate(matched_items):
                item['shipping_fee'] = total_shipping_fee if index == 0 else 0.0
        grand_total = total_product_amount + total_shipping_fee
        balance = user.get('balance', 0) or 0

        preview = {
            'telegram_id': telegram_id,
            '_order_trace_id': (__import__('order_trace').CURRENT.get() or {}).get('trace_id'),
            '_learning_forms': __import__('learning_pipeline').forms_from_validated_order(order_info),
            '_learning_deepseek_analysis': order_info.get('_learning_deepseek_analysis'),
            '_learning_local_parser_result': order_info.get('_learning_local_parser_result'),
            '_learning_ai_called': order_info.get('_learning_ai_called'),
            '_learning_ai_state': order_info.get('_learning_ai_state'),
            '_learning_validator_result': {
                'quantity_scopes': order_info.get('quantity_scopes'),
                'section_subtotals': order_info.get('section_subtotals'),
                'declared_total': order_info.get('declared_total'),
                'computed_total': order_info.get('computed_total'),
                'errors': order_info.get('quantity_scope_errors'),
            },
            '_recipient_generated': order_info.get('_recipient_generated') is True,
            '_recipient_source': order_info.get('_recipient_source'),
            '_recipient_learning_excluded': order_info.get('_recipient_learning_excluded') is True,
            'user': user,
            'items': matched_items,
            'recipient_name': order_info.get('recipient') or order_info.get('recipient_name'),
            'recipient_phone': order_info.get('phone') or order_info.get('recipient_phone'),
            'recipient_address': order_info.get('address') or order_info.get('recipient_address'),
            'total_amount': total_product_amount,
            'total_shipping_fee': total_shipping_fee,
            'grand_total': grand_total,
            'balance': balance,
            'raw_text': order_info.get('raw_text'),
            'normalized_text': order_info.get('normalized_text'),
            'corrections': order_info.get('corrections') or [],
            'warnings': (order_info.get('warnings') or []) + fulfillment_actions,
            'fulfillment_actions': fulfillment_actions,
            'ai_notes': order_info.get('ai_notes') or [],
            'confidence': order_info.get('confidence'),
            'unresolved_fields': order_info.get('unresolved_fields') or [],
            'total_units': total_units,
            'package_count': package_rules.final_package_count(matched_items),
            'units_per_package': 6,
        }
        # Only an explicit directive naming a real accessible warehouse is
        # authoritative. Customer labels such as G8仓 remain mere notes.
        raw_source=str(order_info.get('raw_text') or '')
        directives=re.findall(r'(?m)^\s*(?:发货仓库|指定仓库|仓库)\s*[:：]\s*([^\n]+?)\s*$',raw_source)
        requested=[w for w in warehouses if w.get('name') in directives]
        if len({w['id'] for w in requested})==1:
            preview['_explicit_warehouse_id']=requested[0]['id']
        if getattr(update, 'effective_chat', None) and getattr(update, 'effective_message', None):
            chat = getattr(update, 'effective_chat', None)
            message = getattr(update, 'effective_message', None)
            preview.update({
                'source_chat_id': str(chat.id) if getattr(chat, 'id', None) is not None else None,
                'source_message_id': getattr(update,'_rainbow_source_message_id',getattr(message, 'message_id', None)),
                'source_chat_type': getattr(chat, 'type', None),
                'source_chat_title': (
                    getattr(chat, 'title', None)
                    or getattr(chat, 'full_name', None)
                    or getattr(chat, 'username', None)
                ),
            })
        return preview

    async def _send_ai_flavor_choice(self, update, context, proposal):
        pending=context.user_data['pending_ai_flavor']
        from datetime import datetime, timezone
        pending.setdefault('_confirmation_started_at', datetime.now(timezone.utc).isoformat())
        pending['proposal']=proposal
        import secrets
        revision=secrets.token_hex(4)
        pending['_button_revision']=revision
        pending.setdefault('source',dict(
            source_chat_id=str(update.effective_chat.id),
            source_message_id=getattr(update,'_rainbow_source_message_id',getattr(update.effective_message,'message_id',None)),
            source_chat_type=update.effective_chat.type,
            source_chat_title=getattr(update.effective_chat,'title',None)))
        suffix=':'+context.user_data['_group_draft_token'] if context.user_data.get('_group_draft_token') else ''
        choices=proposal['choices']
        buttons=[[InlineKeyboardButton(choice,
            callback_data=f'ai_flavor:{index}~{revision}{suffix}')]
            for index,choice in enumerate(choices)]
        buttons.append([InlineKeyboardButton('取消订单',callback_data='ai_flavor:cancel~'+revision+suffix)])
        text=('请选择正确口味。\n分类：'+html.escape(proposal['category'])+
              '\n你填写：'+html.escape(proposal['raw_flavor'])+
              '\n建议替换为：'+ '、'.join(html.escape(c) for c in choices)+
              '\n确认后按本单原数量继续录单；取消则不录单、不扣库存。'+
              '\n此确认仅影响本单，通用叫法仍需后台审核。')
        if pending.get('_local_typo_confirmation') and proposal.get('kind')!='alternate':
            item=pending['result']['items'][proposal['index']]
            reference='品类('+proposal['category']+')口味('+proposal['raw_flavor']+')'
            text=('⚠️ 以下商品需要确认：\n• '+html.escape(reference)+' ×'+str(item['quantity'])+
                  '：商品不存在：'+html.escape(reference)+'；是否指：'+
                  '、'.join(html.escape(c)+'（后台原文）' for c in choices)+'？（未自动替换）'+
                  '\n\n确认后按原数量继续；取消则不录单、不扣库存、不记账。'+
                  '\n此确认仅限本单，通用叫法仍须后台审核。')
        query=getattr(update,'callback_query',None)
        if query:
            sent=await query.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(buttons))
        else:
            sent=await update.message.reply_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(buttons))
        from draft_button_evidence import record
        record(context.user_data,InlineKeyboardMarkup(buttons),getattr(sent,'message_id',None))

    async def handle_ai_flavor_callback(self, update, context):
        query=update.callback_query
        await query.answer()
        parts=str(query.data).split(':')
        if not await self._current_order_eligibility(update):
            await query.answer('下单资格已失效，未执行口味确认。');return
        if self._is_group_chat(update) or len(parts)==3:
            if len(parts)!=3:
                await query.answer('按钮已失效，请使用当前草稿的按钮。');return
            account=await self._api_call('get_telegram_account',telegram_id=str(update.effective_user.id))
            draft=await self._api_call('get_group_draft',update.effective_chat.id,parts[2],sender_id=str(update.effective_user.id),include_flavor_expired=True)
            if isinstance(draft,dict) and draft.get('status')=='flavor_expired':
                from flavor_draft_expiry import MESSAGE
                await query.answer(MESSAGE);return
            if not isinstance(account,dict) or not isinstance(draft,dict):
                await query.answer('草稿已处理、过期或无操作权限。');return
            from telegram_order_context import AccountUpdate,DraftContext
            scoped=DraftContext(context,draft)
            try:
                await self._apply_ai_flavor_choice(AccountUpdate(update,account,draft),scoped,parts[1])
            finally:
                completed=not any(scoped.user_data.get(k) for k in ('pending_ai_flavor','pending_recipient_order','pending_order','pending_manual_review'))
                saved,_=await self._api_call('save_group_draft',update.effective_chat.id,draft['token'],scoped.user_data,completed,sender_id=str(update.effective_user.id))
                if not saved:raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')
        else:
            await self._apply_ai_flavor_choice(update,context,parts[1])

    def _draft_cancel_keyboard(self, context):
        token=context.user_data.get('_group_draft_token')
        if not token:
            token=self._private_draft_cancel_token(context)
        return InlineKeyboardMarkup([[InlineKeyboardButton('取消订单',
            callback_data='draft_cancel:'+token)]])

    def _private_draft_cancel_token(self, context):
        import hashlib, json
        keys=('pending_ai_flavor','pending_recipient_order','pending_order','pending_manual_review','quick_order_item')
        snapshot={key:context.user_data.get(key) for key in keys if context.user_data.get(key)}
        return 'p'+hashlib.sha256(json.dumps(snapshot,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()[:16]

    async def handle_draft_cancel(self, update, context):
        query=update.callback_query
        await query.answer()
        scoped=context
        draft=None
        if self._is_group_chat(update) or (':' in str(query.data) and not re.fullmatch(r'p[a-f0-9]{16}',str(query.data).split(':',1)[1])):
            parts=str(query.data).split(':')
            if len(parts)!=2:
                await query.answer('按钮已失效，请使用当前草稿的按钮。');return
            draft=await self._api_call('get_group_draft',update.effective_chat.id,parts[1],sender_id=str(update.effective_user.id),
                audit_event_id=getattr(update,'update_id',None),audit_action='draft_cancel',audit_message_id=getattr(query.message,'message_id',None))
            if not isinstance(draft,dict):
                await query.answer('草稿已处理、过期或无操作权限。');return
            from telegram_order_context import DraftContext
            scoped=DraftContext(context,draft)
        elif str(query.data) != 'draft_cancel:'+self._private_draft_cancel_token(context):
            await query.message.reply_text('该草稿按钮已过期，请使用当前订单的按钮。')
            return
        keys=('pending_ai_flavor','pending_recipient_order','pending_order','pending_manual_review','quick_order_item')
        if not any(scoped.user_data.get(k) for k in keys):
            await query.message.reply_text('订单已录入，如需取消订单或修改收件信息/商品信息，请联系客服处理。')
            return
        for key in keys:scoped.user_data.pop(key,None)
        if draft:
            saved,_=await self._api_call('save_group_draft',update.effective_chat.id,draft['token'],scoped.user_data,True,sender_id=str(update.effective_user.id))
            if not saved:raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')
        await query.edit_message_text('❌ 本次订单已取消',reply_markup=None)

    async def _apply_ai_flavor_choice(self,update,context,selection):
        pending=context.user_data.get('pending_ai_flavor')
        action,_,revision=str(selection).partition('~')
        if pending and pending.get('_button_revision') and revision!=pending['_button_revision']:
            await update.callback_query.answer('这是上一轮口味确认按钮，请使用当前按钮。');return
        selection=action
        if pending and pending.get('_choice_in_progress'):
            await update.callback_query.answer('本次确认正在处理，请勿重复点击。');return
        if pending:pending['_choice_in_progress']=True
        try:
            return await self._apply_ai_flavor_choice_once(update,context,selection)
        except Exception:
            if context.user_data.get('pending_ai_flavor') is pending:
                context.user_data.pop('pending_ai_flavor',None)
                self._retire_noninteractive_failure(context)
            # Queue/commit-proof recovery owns the receipt if a business
            # commit may already exist; never invent a "not recorded" claim.
            raise
        finally:
            if pending:pending.pop('_choice_in_progress',None)

    async def _apply_ai_flavor_choice_once(self,update,context,selection):
        pending=context.user_data.get('pending_ai_flavor')
        if not pending:
            await update.callback_query.answer('口味确认已处理或过期，请使用当前订单的按钮。')
            return
        if selection=='cancel':
            context.user_data.pop('pending_ai_flavor',None)
            self._retire_noninteractive_failure(context)
            await update.callback_query.edit_message_text('❌ 本次订单已取消',reply_markup=None)
            return
        proposal=pending['proposal']
        try:choice=proposal['choices'][int(selection)] if int(selection)>=0 else None
        except (IndexError,ValueError,TypeError):return
        if choice is None:return
        query=update.callback_query
        if hasattr(query,'edit_message_reply_markup'):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as error:
                if 'message is not modified' not in str(error).casefold():raise
            marker=context.user_data.get('_inline_confirmation')
            if isinstance(marker,dict) and marker.get('message_id')==getattr(query.message,'message_id',None):
                context.user_data['_inline_confirmation']=dict(marker,active=False)
        from ai_flavor_decision import proposals,authorize
        products,_,_=await self._visible_catalog_for_user_async(update)
        # Revalidate the actual current catalog before accepting customer choice.
        current=proposals(pending['result'],pending['raw_text'],products,pending['confirmed'])
        valid=next((p for p in current if p['index']==proposal['index']),None)
        # Another completed order can teach an audited Alias while this
        # customer is looking at the confirmation prompt.  In that case the
        # fresh proposal list intentionally becomes empty because the raw
        # flavor now resolves normally.  Treat that stronger, current-catalog
        # resolution as confirmation only when it resolves to the exact
        # displayed choice and the category/product are still live.  Without
        # this bridge the click closes the draft without committing an order.
        if valid is None and choice in proposal.get('choices',()):
            from deepseek_resolver import resolve_category,resolve_flavor
            item=(pending['result'].get('alternates') or [])[proposal['alternate_index']] \
                if proposal.get('kind')=='alternate' else \
                (pending['result'].get('items') or [])[proposal['index']]
            raw_category=str(item.get('raw_category') or item.get('category') or '').strip()
            raw_flavor=str(item.get('raw_flavor') or item.get('flavor') or '').strip()
            live=[p for p in products if p.get('name') and p.get('is_active',True)
                  and not p.get('is_archived')]
            categories=list(dict.fromkeys(p.get('parent_name') for p in live if p.get('parent_name')))
            category,_=resolve_category(raw_category,categories)
            pool=[p for p in live if p.get('parent_name')==category]
            canonical,_=resolve_flavor(category,raw_flavor,
                list(dict.fromkeys(p['name'] for p in pool)),products=pool) if category else (None,None)
            if canonical==choice and any(p['name']==choice for p in pool):
                valid=dict(proposal,category=category,choices=[choice],
                    product_ids={choice:[p['id'] for p in pool if p['name']==choice]})
        if not valid or choice not in valid['choices']:
            await update.callback_query.edit_message_text('商品目录已变化，请重新下单。')
            context.user_data.pop('pending_ai_flavor',None)
            return
        pending['result']=authorize(pending['result'],valid,choice)
        selected=(pending['result']['alternates'][valid['alternate_index']] if valid.get('kind')=='alternate'
                  else pending['result']['items'][valid['index']])
        selected['_customer_confirmed']=True
        import hashlib,json
        proof=dict(category=valid['category'],raw_flavor=valid['raw_flavor'],standard=choice,
            product_ids=valid['product_ids'][choice],revision=pending['_button_revision'],
            callback_ref=hashlib.sha256(str(getattr(query,'id','')).encode()).hexdigest(),
            source_ref=hashlib.sha256(json.dumps(pending.get('source') or {},sort_keys=True).encode()).hexdigest())
        pending.setdefault('_confirmed_mappings',[]).append(proof)
        pending['confirmed'].append(valid['index'])
        pending.setdefault('resolved', {})[str(valid['index'])] = choice
        queued=pending.get('pending_queue')
        more=([p for p in queued if p['index'] not in pending['confirmed']] if queued is not None
              else proposals(pending['result'],pending['raw_text'],products,pending['confirmed']))
        if more:
            await self._send_ai_flavor_choice(update,context,more[0]);return
        info,errors,detail=await recognition_work(self._validate_ai_order_against_catalog,
            pending['result'],pending['raw_text'],products,customer_confirmed=tuple(pending['confirmed']),
            **({'contact_override':pending['contact']} if pending.get('contact') else {}))
        context.user_data.pop('pending_ai_flavor',None)
        if errors:
            # A consumed confirmation that still fails is terminal. Do not
            # reopen a contact/quantity draft or leave its old buttons live.
            self._retire_noninteractive_failure(context)
            text='⚠️ 本次订单仍需修正：\n'+'\n'.join('• '+str(error) for error in errors)
            text+='\n\n本次未录单、未扣库存、未记账；本次确认已结束，请修正后重新发送完整订单。'
            await update.callback_query.edit_message_text(text,reply_markup=None)
            return
        info.update(pending.get('_learning_meta') or {})
        preview=await self._build_order_preview(update,info)
        if preview:
            preview.update(pending['source'])
            try:
                from brand_aliases import learn_validated
                learn_validated(pending['result'],preview,products,confirmations=pending.get('_confirmed_mappings',()))
            except Exception as exc:
                logger.warning('CUSTOMER_CONFIRMED_AI_LEARNING_FAILED type=%s',type(exc).__name__)
            preview['_receipt_message']=update.callback_query.message
            await self._send_order_preview(update,context,preview)

    async def _record_automatic_order(self, update, context, preview):
        """Only the transactional API creates business facts; send once afterward."""
        if not await self._current_order_eligibility(update):
            return
        success, order = await self._api_call(
            'create_order', telegram_id=preview['telegram_id'],
            items=[{key:item.get(key) for key in (
                'product_id','product_name','parent_name','quantity','warehouse_name')}
                for item in (preview.get('_ordinary_items',preview['items']))],
            recipient_info={key:preview[key] for key in (
                'recipient_name','recipient_phone','recipient_address')},
            source_info={key:preview.get(key) for key in (
                'source_chat_id','source_message_id','source_chat_type','source_chat_title')},
            automatic_order=True,
            **({'proxy_info':context.user_data['_proxy_confirmation'],
                'expected_quote':{key:preview[key] for key in ('items','total_amount','total_shipping_fee','grand_total')}}
               if context.user_data.get('_proxy_confirmation') else {}),
            cross_warehouse_confirmed=bool(preview.get('_cross_warehouse_confirmed')),
            explicit_warehouse_id=preview.get('_explicit_warehouse_id'),
            **({'bundle_requests':preview['bundle_requests']} if preview.get('bundle_requests') else {}),
        )
        context.user_data.pop('pending_order', None)
        context.user_data.pop('pending_order_source', None)
        if not success:
            error=order.get('error') if isinstance(order,dict) else order
            if isinstance(error,dict) and error.get('code')=='CROSS_WAREHOUSE_REQUIRED':
                pending={key:value for key,value in preview.items() if key!='_receipt_message'}
                pending['_cross_warehouse_pending']=True
                token=context.user_data.get('_group_draft_token')
                payload=None
                if not token:
                    # Reuse the existing durable owner+chat+source-message
                    # draft store for private cross-warehouse confirmations.
                    # No new schema and no process-memory-only confirmation.
                    ok,result=await self._api_call('open_group_draft',
                        update.effective_chat.id,preview['source_message_id'],
                        str(update.effective_user.id),new_order=True)
                    draft=result.get('draft') if ok and isinstance(result,dict) else None
                    if not draft:
                        await update.effective_message.reply_text('当前订单确认暂时不可用，请联系客服核实；尚未录单。')
                        return
                    token=draft['token'];payload={'pending_order':pending}
                    saved,_=await self._api_call('save_group_draft',update.effective_chat.id,
                        token,payload,False,sender_id=str(update.effective_user.id))
                    if not saved:return
                else:
                    context.user_data['pending_order']=pending
                suffix=':'+token
                prompt=await update.effective_message.reply_text(
                    '⚠️ 当前订单产品存在于不同仓库，是否分单发货？',
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton('确认分单',callback_data='order_confirm'+suffix),
                        InlineKeyboardButton('取消订单',callback_data='order_cancel'+suffix)]]))
                if payload is not None and isinstance(getattr(prompt,'message_id',None),int):
                    payload['_reply_message_ids']=[prompt.message_id]
                    await self._api_call('save_group_draft',update.effective_chat.id,
                        token,payload,False,sender_id=str(update.effective_user.id))
                return
            if re.search(r'缺货|库存不足|暂不出货|预警库存|库存为\s*0',str(error)):
                failed=dict(preview,total_amount=0,grand_total=0)
                await update.effective_message.reply_text(self._agent_group_order_text(failed,error=str(error)),parse_mode='HTML')
                return
            if str(error).startswith(('❌ 下单失败','❌ 录单失败')):
                await update.effective_message.reply_text(str(error))
                return
            if '已处理' in str(error) or 'MESSAGE_ALREADY_PROCESSED' in str(error):
                return
            # Never expose transport/server details to the customer.
            if error in ('请求超时', '无法连接到 API 服务器，请检查是否已启动'):
                await update.effective_message.reply_text('本次录单结果暂未确认，请勿重复提交。请查看订单记录或联系客服核实。')
            else:
                await update.effective_message.reply_text('本次未能完成录单，请检查商品库存或联系客服核实；请勿重复提交。')
            return
        if not isinstance(order,dict) or not order.get('items') or any(
            key not in order for key in ('total_amount','total_shipping_fee','grand_total','user_debt')):
            logger.error('AUTOMATIC_RECEIPT_FACTS_INCOMPLETE')
            return
        facts=dict(order,_automatic_receipt=True,_recipient_generated=preview.get('_recipient_generated') is True,
            source_message_id=preview['source_message_id'],source_chat_type=preview['source_chat_type'],
            fulfillment_actions=preview.get('fulfillment_actions') or [])
        try:
            if preview.get('_receipt_message_id') and not preview.get('_receipt_message'):
                facts['_receipt_message_id']=preview['_receipt_message_id']
                facts['_receipt_chat_id']=preview.get('_receipt_chat_id') or update.effective_chat.id
            if preview.get('_receipt_message'):
                facts['_receipt_message']=preview['_receipt_message']
            # Receipt retries belong to Telegram delivery only. No call back into
            # parsing, allocation or create_order on a send error.
            from order_trace import stage, first_response
            with stage('TELEGRAM_SEND'):
                await self._send_order_confirmation(context.bot,update.effective_chat.id,facts)
            first_response()
            context.user_data.pop('_group_receipt_message_id',None)
            context.user_data.pop('_group_receipt_chat_id',None)
            context.user_data['_group_receipt_closed']=True
        finally:
            try:
                from learning_pipeline import record_committed
                from order_trace import stage
                with stage('POST_COMMIT_LEARNING'):
                    await asyncio.to_thread(record_committed,preview,order)
            except Exception as exc:
                logger.warning('COMMITTED_LEARNING_DEFERRED type=%s',type(exc).__name__)

    async def _send_order_preview(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        preview: Dict,
        source: str = '',
    ):
        """发送下单预览并等待确认"""
        if self._is_agent_group(update):
            receipt_message_id=context.user_data.get('_group_receipt_message_id')
            if receipt_message_id and not context.user_data.get('_group_receipt_closed'):
                preview.setdefault('_receipt_message_id',receipt_message_id)
                preview.setdefault(
                    '_receipt_chat_id',
                    context.user_data.get('_group_receipt_chat_id') or update.effective_chat.id,
                )
        if not self._is_wholesale_bot() and preview.get('source_message_id'):
            await self._record_automatic_order(update, context, preview)
            return
        # 按仓库分组展示
        if self._is_agent_group(update) and not getattr(update,'_rainbow_account',None):
            await self._record_agent_group_order(update, context, preview)
            return
        by_wh = {}
        for item in preview['items']:
            by_wh.setdefault(item['warehouse_name'], []).append(item)

        message = "🧾 <b>请确认订单</b>\n\n"
        corrections = preview.get('corrections') or []
        if corrections:
            message += "🧹 <b>系统已自动整理</b>\n"
            message += "".join(
                f"  • {html.escape(str(item))}\n" for item in corrections[:5]
            )
            message += "\n"
        warnings = preview.get('warnings') or []
        if warnings:
            message += "⚠️ <b>请重点核对</b>\n"
            message += "".join(
                f"  • {html.escape(str(item))}\n" for item in warnings[:5]
            )
            message += "\n"
        for wh, items in by_wh.items():
            message += f"🏭 <b>{html.escape(str(wh))}</b>\n"
            for item in items:
                display_name = html.escape(
                    str(item.get('display_name') or item['product_name'])
                )
                message += (
                    f"  • {display_name} x {item['quantity']} "
                    f"(¥{item['unit_price']:.2f}) | 现库存 {item['current_stock']}\n"
                )
            fee = sum(float(item.get('shipping_fee') or 0) for item in items)
            message += f"  🚚 运费: ¥{fee:.2f}\n\n"

        package_count = int(preview.get("package_count") or 0)
        units_per_package = int(preview.get("units_per_package") or 0)
        if package_count > 1 and units_per_package:
            # 快递要求每 N 支一个包裹，客户在确认页就该知道会拆几个包
            message += (
                f"📦 共 {preview.get('total_units')} 支，将分 <b>{package_count} 个包裹</b>发出"
                f"（每 {units_per_package} 支一个包裹，快递要求）\n\n"
            )

        message += (
            f"💰 商品合计: ¥{preview['total_amount']:.2f}\n"
            f"🚚 运费合计: ¥{preview['total_shipping_fee']:.2f}\n"
            f"✅ 应付总额: ¥{preview['grand_total']:.2f}\n"
            f"💳 预存余额: ¥{preview['balance']:.2f}\n\n"
            f"🏠 收件人: {html.escape(str(preview['recipient_name'] or ''))}\n"
            f"📞 电话: {html.escape(str(preview['recipient_phone'] or ''))}\n"
            f"📍 地址: {html.escape(str(preview['recipient_address'] or ''))}\n\n"
        )

        message += (
            "确认后将：生成订单 → 按你的单价记账 → 自动减库存\n"
            "结账方式：人工收账（可累计欠款，不强制每单一结）"
        )
        context.user_data['pending_order'] = preview
        context.user_data['pending_order_source'] = source or ''

        draft_suffix=':'+context.user_data['_group_draft_token'] if context.user_data.get('_group_draft_token') else ''
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 确认下单", callback_data="order_confirm"+draft_suffix),
                InlineKeyboardButton("取消订单", callback_data="order_cancel"+draft_suffix),
            ]
        ])
        if getattr(update, 'message', None):
            await self._send_chunked_message(update.message.reply_text, message, parse_mode='HTML', reply_markup=keyboard)
        elif update.callback_query and update.callback_query.message:
            await self._send_chunked_message(update.callback_query.message.reply_text, message, parse_mode='HTML', reply_markup=keyboard)



    async def products_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """商品列表：先选分类，再看商品与单价（参考截图双列分类）"""
        parents = (await self._api_call('get_parent_categories')) if hasattr(api_client, 'get_parent_categories') else []
        # fallback via products parents endpoint through generic request
        if not parents:
            ok, data = (await self._api_call('_make_request', 'GET', '/api/products/parents'))
            parents = data if ok and isinstance(data, list) else []
        if not parents:
            # 无分类时直接展示商品
            await self.products(update, context)
            return
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        stock_map = (await self._product_stock_map_async(visible_warehouses, products))
        if self._is_wholesale_bot():
            parents = [p for p in parents if str(p.get('name') or '').strip() != "默认分类"]
        child_map: Dict[int, List[Dict]] = {}
        for product in products:
            if product.get('is_parent'):
                continue
            try:
                pid = int(product.get('parent_id') or 0)
            except Exception:
                continue
            child_map.setdefault(pid, []).append(product)

        def parent_sort(parent):
            try:
                parent_id = int(parent.get('id') or 0)
            except Exception:
                parent_id = 0
            children = child_map.get(parent_id, [])
            has_available = False
            total_qty = 0
            for product in children:
                stock = int(stock_map.get(product.get('id'), 0) or 0)
                total_qty += stock
                threshold = normalize_threshold(product.get('low_stock_threshold'))
                if not is_stock_paused(stock, threshold):
                    has_available = True
            return (0 if has_available else 1, -total_qty, str(parent.get('name') or ''))

        # 不展示当前用户没有任何可见商品的分类（例如只属于旺财仓的分类）。
        parents = [
            parent for parent in parents
            if child_map.get(int(parent.get('id') or 0), [])
        ]
        parents = sorted(parents, key=parent_sort)
        if not parents:
            await self._safe_reply(update, "暂无可选择型号")
            return
        rows = []
        row = []
        for p in parents:
            name = p.get('name') or f"分类{p.get('id')}"
            # skip empty default maybe keep
            btn = InlineKeyboardButton(f"型号 {name}", callback_data=f"cat:{p.get('id')}")
            row.append(btn)
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton(self.BTN_BACK, callback_data="menu:back")])
        text = "请选择型号："
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

    async def show_category_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE, parent_id: int):
        await self.show_category_products_page(update, context, parent_id, page=1)

    async def show_category_products_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE, parent_id: int, page: int = 1):
        telegram_id = str(update.effective_user.id)
        success, user = (await self._api_call('create_or_get_user', 
            telegram_id,
            update.effective_user.username,
            getattr(update.effective_user, 'full_name', None),
        ))
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        stock_map = (await self._product_stock_map_async(visible_warehouses, products))
        price_map = {}
        if success and user:
            detail = (await self._api_call('get_user_prices', user['id'])) or {}
            for item in detail.get('prices', []):
                price_map[item['product_id']] = item
        items = [p for p in products if str(p.get('parent_id') or '') == str(parent_id) and not p.get('is_parent')]
        def product_sort(product):
            stock = int(stock_map.get(product.get('id'), 0) or 0)
            threshold = normalize_threshold(product.get('low_stock_threshold'))
            paused = is_stock_paused(stock, threshold)
            return (0 if not paused else 1, -stock, str(product.get('name') or ''))
        items = sorted(items, key=product_sort)
        parent_name = None
        ok, parents = (await self._api_call('_make_request', 'GET', '/api/products/parents'))
        if ok and isinstance(parents, list):
            for p in parents:
                if str(p.get('id')) == str(parent_id):
                    parent_name = p.get('name')
                    break
        title = f"型号：<b>{parent_name or '未分类'}</b>"
        page = max(1, int(page or 1))
        total_pages = max(1, (len(items) + self.PRODUCT_PAGE_SIZE - 1) // self.PRODUCT_PAGE_SIZE)
        page = min(page, total_pages)
        start = (page - 1) * self.PRODUCT_PAGE_SIZE
        page_items = items[start:start + self.PRODUCT_PAGE_SIZE]
        if not items:
            text = title + "\n\n该分类暂无商品"
            rows = []
        else:
            lines = [title, ""]
            rows = []
            for product in page_items:
                info = price_map.get(product['id'])
                price = float(info['unit_price']) if info else float(product.get('regular_price') or product.get('price') or 0)
                stock = int(stock_map.get(product['id'], 0) or 0)
                threshold = normalize_threshold(product.get('low_stock_threshold'))
                dot = "🔴" if stock <= 0 else ("🟡" if threshold > 0 and stock <= threshold else "🟢")
                name = product.get('name') or f"商品{product.get('id')}"
                paused = is_stock_paused(stock, threshold)
                suffix = " | 暂停售卖" if paused else (" | 少量" if dot == "🟡" else "")
                label = f"{dot} 口味 {name} | {price:.2f} | 库存 {stock}{suffix}"
                callback = "noop" if paused else f"prod:{product.get('id')}:{parent_id}:{page}"
                rows.append([InlineKeyboardButton(label, callback_data=callback)])
            lines.append("")
            if stock_pause_mode() == "zero":
                lines.append("先选型号，再选口味；🔴 库存为0时暂停售卖，🟡 表示少量但仍可下单。")
            else:
                lines.append("先选型号，再选口味；🟡 表示到达后台预警值，暂停售卖。")
            text = chr(10).join(lines)
            if total_pages > 1:
                nav = []
                if page > 1:
                    nav.append(InlineKeyboardButton("上一页⬅️", callback_data=f"cat:{parent_id}:{page - 1}"))
                nav.append(InlineKeyboardButton(f"{page} / {total_pages}", callback_data="noop"))
                if page < total_pages:
                    nav.append(InlineKeyboardButton("下一页➡️", callback_data=f"cat:{parent_id}:{page + 1}"))
                rows.append(nav)
        rows.append([InlineKeyboardButton("返回上级", callback_data="menu:products")])
        kb = InlineKeyboardMarkup(rows)
        q = update.callback_query
        if q:
            await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)

    def _product_stock_map(self, warehouses: Optional[List[Dict]] = None, products: Optional[List[Dict]] = None) -> Dict[int, int]:
        if warehouses is None:
            warehouses = self._visible_warehouses(api_client.get_all_warehouses())
        allowed_ids = {int(row.get('id') or 0) for row in warehouses}
        products = products if products is not None else (api_client.get_products() or [])
        defaults = {
            int(row.get('id') or 0): int(row.get('default_warehouse_id') or 0)
            for row in products
            if row.get('id') is not None
        }
        stocks = api_client.get_stock_filtered(include_inactive=True) or []
        stock_map: Dict[int, int] = {}
        for row in stocks:
            try:
                pid = int(row.get('product_id'))
            except Exception:
                continue
            warehouse_id = int(row.get('warehouse_id') or 0)
            if warehouse_id not in allowed_ids or defaults.get(pid) != warehouse_id:
                continue
            value = row.get('sellable_quantity')
            if value is None:
                value = row.get('quantity')
            stock_map[pid] = stock_map.get(pid, 0) + int(value or 0)
        return stock_map

    def _product_price_for_user(self, product: Dict, user: Optional[Dict], prices: Optional[Dict] = None) -> float:
        if user:
            detail = prices if prices is not None else (api_client.get_user_prices(user['id']) or {})
            for item in detail.get('prices', []):
                if item.get('product_id') == product.get('id'):
                    return float(item.get('unit_price') or 0)
        level = (user or {}).get('agent_level', 0) or 0
        if level == 1:
            return float(product.get('level2_agent_price') or product.get('regular_price') or product.get('price') or 0)
        if level == 2:
            return float(product.get('level1_agent_price') or product.get('regular_price') or product.get('price') or 0)
        return float(product.get('regular_price') or product.get('price') or 0)

    def _wholesale_cart(self, context: ContextTypes.DEFAULT_TYPE) -> Dict[str, int]:
        """获取并清理当前 Telegram 会话里的批发购物车。"""
        raw = context.user_data.get('wholesale_cart')
        if not isinstance(raw, dict):
            raw = {}
            context.user_data['wholesale_cart'] = raw

        normalized: Dict[str, int] = {}
        for product_id, quantity in raw.items():
            try:
                pid = str(int(product_id))
                qty = max(0, int(quantity or 0))
            except (TypeError, ValueError):
                continue
            if qty > 0:
                normalized[pid] = qty
        if raw != normalized:
            raw.clear()
            raw.update(normalized)
        return raw

    @staticmethod
    def _wholesale_cart_counts(cart: Dict[str, int]) -> Tuple[int, int]:
        kinds = sum(1 for quantity in cart.values() if int(quantity or 0) > 0)
        total_quantity = sum(max(0, int(quantity or 0)) for quantity in cart.values())
        return kinds, total_quantity

    def _set_wholesale_cart_quantity(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        product_id: int,
        quantity: int,
    ) -> Dict[str, int]:
        cart = self._wholesale_cart(context)
        key = str(int(product_id))
        quantity = max(0, int(quantity or 0))
        if quantity:
            cart[key] = quantity
        else:
            cart.pop(key, None)
        return cart

    def _cart_price_context(self, update: Update) -> Tuple[Optional[Dict], Dict[int, float]]:
        """一次读取当前用户及其专属价格，避免每个口味重复请求。"""
        user = None
        price_map: Dict[int, float] = {}
        if not update.effective_user:
            return user, price_map
        ok, user = api_client.create_or_get_user(
            str(update.effective_user.id),
            update.effective_user.username,
            getattr(update.effective_user, 'full_name', None),
        )
        if not ok or not user:
            return None, price_map
        detail = api_client.get_user_prices(user['id']) or {}
        for row in detail.get('prices', []):
            try:
                price_map[int(row.get('product_id'))] = float(row.get('unit_price') or 0)
            except (TypeError, ValueError):
                continue
        return user, price_map

    @staticmethod
    def _cart_unit_price(product: Dict, user: Optional[Dict], price_map: Dict[int, float]) -> float:
        try:
            product_id = int(product.get('id') or 0)
        except (TypeError, ValueError):
            product_id = 0
        if product_id in price_map:
            return float(price_map[product_id])
        level = int((user or {}).get('agent_level') or 0)
        if level == 1:
            return float(product.get('level2_agent_price') or product.get('regular_price') or product.get('price') or 0)
        if level == 2:
            return float(product.get('level1_agent_price') or product.get('regular_price') or product.get('price') or 0)
        return float(product.get('regular_price') or product.get('price') or 0)

    async def _cart_render(
        self,
        update: Update,
        text: str,
        rows: List[List[InlineKeyboardButton]],
    ):
        keyboard = InlineKeyboardMarkup(rows)
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        elif update.message:
            await update.message.reply_text(
                text,
                parse_mode='HTML',
                reply_markup=keyboard,
            )

    def _cart_parent_categories(self) -> List[Dict]:
        parents = api_client.get_parent_categories() if hasattr(api_client, 'get_parent_categories') else []
        if not parents:
            ok, data = api_client._make_request('GET', '/api/products/parents')
            parents = data if ok and isinstance(data, list) else []
        return [
            parent for parent in (parents or [])
            if str(parent.get('name') or '').strip() != '默认分类'
            and parent.get('is_active', True)
        ]

    async def show_wholesale_cart_categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """批发购物车首页：先挑型号。"""
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        stock_map = (await self._product_stock_map_async(visible_warehouses, products))
        child_map: Dict[int, List[Dict]] = {}
        for product in products:
            if product.get('is_parent') or not product.get('is_active', True):
                continue
            try:
                parent_id = int(product.get('parent_id') or 0)
                product_id = int(product.get('id') or 0)
            except (TypeError, ValueError):
                continue
            stock = int(stock_map.get(product_id, 0) or 0)
            threshold = normalize_threshold(product.get('low_stock_threshold'))
            if stock <= 0 or is_stock_paused(stock, threshold):
                continue
            child_map.setdefault(parent_id, []).append(product)

        parents = [
            parent for parent in (await asyncio.to_thread(self._cart_parent_categories))
            if child_map.get(int(parent.get('id') or 0), [])
        ]
        parents.sort(
            key=lambda parent: (
                -sum(
                    int(stock_map.get(item.get('id'), 0) or 0)
                    for item in child_map.get(int(parent.get('id') or 0), [])
                ),
                str(parent.get('name') or ''),
            )
        )

        cart = self._wholesale_cart(context)
        kinds, total_quantity = self._wholesale_cart_counts(cart)
        lines = [
            "🛒 <b>批发购物车</b>",
            "",
            f"已选：{kinds} 种 / {total_quantity} 件",
            "请选择型号；进入后在口味右侧用 ➖ / ➕ 调整数量。",
        ]
        rows: List[List[InlineKeyboardButton]] = []
        for parent in parents:
            parent_id = int(parent.get('id') or 0)
            name = parent.get('name') or f"型号{parent_id}"
            rows.append([
                InlineKeyboardButton(
                    f"📦 型号 {name}",
                    callback_data=f"cart:cat:{parent_id}:1",
                )
            ])
        if not rows:
            lines.append("")
            lines.append("暂无可选口味，请联系管理员补货或调整库存预警。")
        rows.append([
            InlineKeyboardButton(
                f"🛒 查看购物车（{kinds}种/{total_quantity}件）",
                callback_data="cart:view",
            )
        ])
        await self._cart_render(update, "\n".join(lines), rows)

    async def show_wholesale_cart_category(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        parent_id: int,
        page: int = 1,
    ):
        """批发选货页：口味在左，数量加减在右。"""
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        stock_map = (await self._product_stock_map_async(visible_warehouses, products))
        parents = (await asyncio.to_thread(self._cart_parent_categories))
        parent = next((item for item in parents if int(item.get('id') or 0) == int(parent_id)), None)
        parent_name = (parent or {}).get('name') or '未分类'
        user, price_map = (await asyncio.to_thread(self._cart_price_context, update))

        items: List[Dict] = []
        for product in products:
            if product.get('is_parent') or not product.get('is_active', True):
                continue
            if int(product.get('parent_id') or 0) != int(parent_id):
                continue
            product_id = int(product.get('id') or 0)
            stock = int(stock_map.get(product_id, 0) or 0)
            threshold = normalize_threshold(product.get('low_stock_threshold'))
            if stock <= 0 or is_stock_paused(stock, threshold):
                continue
            items.append(product)

        items.sort(
            key=lambda product: (
                -int(stock_map.get(product.get('id'), 0) or 0),
                str(product.get('name') or ''),
            )
        )
        page = max(1, int(page or 1))
        total_pages = max(1, (len(items) + self.PRODUCT_PAGE_SIZE - 1) // self.PRODUCT_PAGE_SIZE)
        page = min(page, total_pages)
        page_items = items[(page - 1) * self.PRODUCT_PAGE_SIZE:page * self.PRODUCT_PAGE_SIZE]

        cart = self._wholesale_cart(context)
        kinds, total_quantity = self._wholesale_cart_counts(cart)
        lines = [
            "🛒 <b>批发选货</b>",
            f"型号：<b>{parent_name}</b>",
            f"购物车：{kinds} 种 / {total_quantity} 件",
            "",
            "点击每个口味右侧的 ➖ / ➕ 调整数量。",
        ]
        rows: List[List[InlineKeyboardButton]] = []
        for product in page_items:
            product_id = int(product.get('id') or 0)
            stock = int(stock_map.get(product_id, 0) or 0)
            current_quantity = min(int(cart.get(str(product_id), 0) or 0), stock)
            if current_quantity != int(cart.get(str(product_id), 0) or 0):
                self._set_wholesale_cart_quantity(context, product_id, current_quantity)
                cart = self._wholesale_cart(context)
            price = self._cart_unit_price(product, user, price_map)
            name = product.get('name') or f"口味{product_id}"
            minus_callback = (
                f"cart:qty:{product_id}:-1:{parent_id}:{page}"
                if current_quantity > 0 else "noop"
            )
            plus_callback = (
                f"cart:qty:{product_id}:1:{parent_id}:{page}"
                if current_quantity < stock else "noop"
            )
            rows.append([
                InlineKeyboardButton(
                    f"🟢 {name} ¥{price:.2f} 库存{stock}",
                    callback_data="noop",
                ),
                InlineKeyboardButton("➖", callback_data=minus_callback),
                InlineKeyboardButton(
                    f"数量 {current_quantity}",
                    callback_data=f"cart:input:{product_id}:{parent_id}:{page}:cat",
                ),
                InlineKeyboardButton("➕", callback_data=plus_callback),
            ])

        if not page_items:
            lines.append("")
            lines.append("该型号暂无可选口味（缺货或达到预警停售的口味已隐藏）。")
        if total_pages > 1:
            nav: List[InlineKeyboardButton] = []
            if page > 1:
                nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"cart:cat:{parent_id}:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
            if page < total_pages:
                nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"cart:cat:{parent_id}:{page + 1}"))
            rows.append(nav)
        rows.append([
            InlineKeyboardButton("⬅️ 型号列表", callback_data="cart:browse"),
            InlineKeyboardButton(f"🛒 购物车（{kinds}/{total_quantity}）", callback_data="cart:view"),
        ])
        if total_quantity:
            rows.append([InlineKeyboardButton("🧾 一起结算", callback_data="cart:checkout")])
        await self._cart_render(update, "\n".join(lines), rows)

    async def adjust_wholesale_cart_quantity(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        product_id: int,
        delta: int,
        parent_id: int,
        page: int,
    ):
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        product = next((item for item in products if int(item.get('id') or 0) == int(product_id)), None)
        if not product:
            self._set_wholesale_cart_quantity(context, product_id, 0)
            await self.show_wholesale_cart_category(update, context, parent_id, page)
            return

        cart = self._wholesale_cart(context)
        current_quantity = int(cart.get(str(product_id), 0) or 0)
        stock = int((await self._product_stock_map_async(visible_warehouses, products)).get(product_id, 0) or 0)
        threshold = normalize_threshold(product.get('low_stock_threshold'))
        target_quantity = current_quantity
        if int(delta) < 0:
            target_quantity = max(0, current_quantity - 1)
        elif product.get('is_active', True) and stock > 0 and not is_stock_paused(stock, threshold):
            target_quantity = min(stock, current_quantity + 1)
        self._set_wholesale_cart_quantity(context, product_id, target_quantity)
        await self.show_wholesale_cart_category(update, context, parent_id, page)

    async def show_wholesale_cart_view(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """批发购物车汇总页。"""
        cart = self._wholesale_cart(context)
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        product_map = {str(int(product.get('id') or 0)): product for product in products if product.get('id')}
        stock_map = (await self._product_stock_map_async(visible_warehouses, products))
        user, price_map = (await asyncio.to_thread(self._cart_price_context, update))
        kinds, total_quantity = self._wholesale_cart_counts(cart)
        lines = [
            "🛒 <b>批发购物车</b>",
            "",
        ]
        rows: List[List[InlineKeyboardButton]] = []
        subtotal = 0.0
        for product_id, quantity in sorted(cart.items(), key=lambda row: int(row[0])):
            product = product_map.get(str(product_id))
            if not product:
                lines.append(f"⚠️ 商品 ID {product_id} 已不存在，请清空后重新挑选。")
                continue
            unit_price = self._cart_unit_price(product, user, price_map)
            amount = unit_price * int(quantity)
            subtotal += amount
            stock = int(stock_map.get(int(product_id), 0) or 0)
            threshold = normalize_threshold(product.get('low_stock_threshold'))
            parent_name = product.get('parent_name') or '未分类'
            availability = ""
            if not product.get('is_active', True) or is_stock_paused(stock, threshold):
                availability = " ⚠️当前停售，结算时会再次校验"
            elif int(quantity) > stock:
                availability = f" ⚠️库存仅{stock}"
            lines.append(
                f"• 品类({parent_name})口味({product.get('name')}) x{int(quantity)} "
                f"/ 库存 {stock} = ¥{amount:.2f}{availability}"
            )

            parent_id = int(product.get('parent_id') or 0)
            rows_for_item = [
                InlineKeyboardButton(
                    f"{product.get('name')} {int(quantity)}/{stock}",
                    callback_data=f"cart:input:{int(product_id)}:{parent_id}:1:view",
                ),
                InlineKeyboardButton(
                    "➖",
                    callback_data=(
                        f"cart:qty:{int(product_id)}:-1:{parent_id}:1"
                        if int(quantity) > 0 else "noop"
                    ),
                ),
                InlineKeyboardButton(
                    "➕",
                    callback_data=(
                        f"cart:qty:{int(product_id)}:1:{parent_id}:1"
                        if int(quantity) < stock else "noop"
                    ),
                ),
            ]
            rows.append(rows_for_item)

        if not cart:
            lines.append("还没有选择商品。")
        else:
            lines.extend([
                "",
                f"共 {kinds} 种 / {total_quantity} 件",
                f"💰 商品小计：¥{subtotal:.2f}",
                "🚚 运费将在结算时计算：普通 0.8 元/根；烟弹 0.5 元/颗。",
                "点击「一起结算」后发送地址、姓名、手机号。",
            ])

        rows.extend([[
            InlineKeyboardButton("🛍 继续挑选", callback_data="cart:browse"),
            InlineKeyboardButton(
                "🧾 一起结算" if cart else "🧾 一起结算（空）",
                callback_data="cart:checkout" if cart else "noop",
            ),
        ]])
        if cart:
            rows.append([InlineKeyboardButton("🗑 清空购物车", callback_data="cart:clear")])
        await self._cart_render(update, "\n".join(lines), rows)

    async def start_wholesale_cart_checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """锁定当前购物车商品，等待用户发送收货信息。"""
        cart = self._wholesale_cart(context)
        if not cart:
            await self.show_wholesale_cart_view(update, context)
            return
        context.user_data.pop('wholesale_cart_quantity_input', None)
        snapshot = [
            {'product_id': int(product_id), 'quantity': int(quantity)}
            for product_id, quantity in cart.items()
            if int(quantity or 0) > 0
        ]
        if not snapshot:
            await self.show_wholesale_cart_view(update, context)
            return
        context.user_data['wholesale_cart_checkout'] = snapshot
        kinds, total_quantity = self._wholesale_cart_counts(cart)
        text = (
            "🧾 <b>批发购物车结算</b>\n\n"
            f"已锁定：{kinds} 种 / {total_quantity} 件\n"
            "请直接发送三行收货信息：\n"
            "<code>详细地址\n姓名\n手机号</code>\n\n"
            "收到后会生成确认单，确认下单后才会扣库存。"
        )
        rows = [[InlineKeyboardButton("⬅️ 返回购物车", callback_data="cart:view")]]
        await self._cart_render(update, text, rows)

    async def start_wholesale_cart_quantity_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        product_id: int,
        parent_id: int,
        page: int = 1,
        target: str = 'cat',
    ):
        """进入购物车数量输入状态。"""
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        product = next(
            (item for item in products if int(item.get('id') or 0) == int(product_id)),
            None,
        )
        if not product:
            await self._cart_render(
                update,
                "商品不存在或已下架。",
                [[InlineKeyboardButton("⬅️ 返回购物车", callback_data="cart:view")]],
            )
            return
        stock = int((await self._product_stock_map_async(visible_warehouses, products)).get(product_id, 0) or 0)
        cart = self._wholesale_cart(context)
        current_quantity = int(cart.get(str(product_id), 0) or 0)
        target = target if target in ('cat', 'view') else 'cat'
        context.user_data['wholesale_cart_quantity_input'] = {
            'product_id': int(product_id),
            'parent_id': int(parent_id),
            'page': int(page or 1),
            'target': target,
        }
        text = (
            "✍️ <b>填写购买数量</b>\n\n"
            f"口味：{product.get('name')}\n"
            f"当前已选：{current_quantity}\n"
            f"当前库存：{stock}\n\n"
            f"请直接发送数字（0-{stock}），发送 0 将移除该口味。"
        )
        back_callback = (
            f"cart:cat:{int(parent_id)}:{int(page or 1)}"
            if target == 'cat' else 'cart:view'
        )
        await self._cart_render(
            update,
            text,
            [[InlineKeyboardButton("⬅️ 返回", callback_data=back_callback)]],
        )

    async def _handle_wholesale_cart_quantity_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_text: str,
    ) -> bool:
        state = context.user_data.get('wholesale_cart_quantity_input') or {}
        if not state:
            return False
        raw = str(message_text or '').strip()
        if not re.fullmatch(r'\d+', raw):
            await update.message.reply_text("❌ 数量只填写整数，例如：20")
            return True
        quantity = int(raw)
        product_id = int(state.get('product_id') or 0)
        parent_id = int(state.get('parent_id') or 0)
        page = int(state.get('page') or 1)
        target = state.get('target') or 'cat'
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        product = next(
            (item for item in products if int(item.get('id') or 0) == product_id),
            None,
        )
        if not product:
            context.user_data.pop('wholesale_cart_quantity_input', None)
            await update.message.reply_text("❌ 商品不存在或已下架，请重新选择。")
            return True
        stock = int((await self._product_stock_map_async(visible_warehouses, products)).get(product_id, 0) or 0)
        threshold = normalize_threshold(product.get('low_stock_threshold'))
        if quantity > stock:
            await update.message.reply_text(f"❌ 数量不能超过当前库存 {stock}，请重新输入。")
            return True
        if quantity > 0 and (
            not product.get('is_active', True)
            or stock <= 0
            or is_stock_paused(stock, threshold)
        ):
            await update.message.reply_text("❌ 该口味当前已停售，只能填写 0。")
            return True

        self._set_wholesale_cart_quantity(context, product_id, quantity)
        context.user_data.pop('wholesale_cart_quantity_input', None)
        await update.message.reply_text(f"✅ 已设置 {product.get('name')} 数量：{quantity}")
        if target == 'view':
            await self.show_wholesale_cart_view(update, context)
        else:
            await self.show_wholesale_cart_category(update, context, parent_id, page)
        return True

    async def show_product_quick_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, parent_id: int = 0, page: int = 1, quantity: int = 1):
        query = update.callback_query
        telegram_id = str(update.effective_user.id)
        success, user = (await self._api_call('create_or_get_user', 
            telegram_id,
            update.effective_user.username,
            getattr(update.effective_user, 'full_name', None),
        ))
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        product = next((p for p in products if int(p.get('id') or 0) == int(product_id)), None)
        if not product:
            await query.edit_message_text("商品不存在或已下架")
            return
        stock = int((await self._product_stock_map_async(visible_warehouses, products)).get(product_id, 0) or 0)
        threshold = normalize_threshold(product.get('low_stock_threshold'))
        if is_stock_paused(stock, threshold):
            await query.answer(stock_pause_text(stock, threshold), show_alert=True)
            return
        quantity = max(1, min(int(quantity or 1), max(stock, 1)))
        price = (await asyncio.to_thread(self._product_price_for_user, product, user if success else None))
        dot = "🔴" if stock <= 0 else ("🟡" if threshold > 0 and stock <= threshold else "🟢")
        total = price * quantity
        parent_name = product.get('parent_name') or ''
        text = (
            f"型号：<b>{parent_name or '未分类'}</b>\n"
            f"口味：<b>{product.get('name')}</b>\n\n"
            f"价格：{price:.2f} CNY\n"
            f"库存：{dot} {stock}\n"
            f"数量：{quantity}\n"
            f"小计：{total:.2f} CNY\n\n"
            "点击“填写收货信息”后，按提示发送地址 / 姓名 / 手机号即可下单。"
        )
        rows = [
            [
                InlineKeyboardButton("➖", callback_data=f"qty:{product_id}:{max(1, quantity - 1)}:{parent_id}:{page}"),
                InlineKeyboardButton(f"{quantity}", callback_data="noop"),
                InlineKeyboardButton("➕", callback_data=f"qty:{product_id}:{quantity + 1}:{parent_id}:{page}"),
            ],
            [InlineKeyboardButton("填写收货信息", callback_data=f"quick:{product_id}:{quantity}")],
            [InlineKeyboardButton("返回商品列表", callback_data=f"cat:{parent_id}:{page}")],
        ]
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(rows))

    async def start_quick_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, quantity: int):
        query = update.callback_query
        if not await self._current_order_eligibility(update):
            await query.edit_message_text('请先通过代发申请审核后下单；历史订单和账务仍可查看。')
            return
        products, visible_warehouses, _user = (await self._visible_catalog_for_user_async(update))
        product = next((p for p in products if int(p.get('id') or 0) == int(product_id)), None)
        if not product:
            await query.edit_message_text("商品不存在或已下架")
            return
        stock = int((await self._product_stock_map_async(visible_warehouses, products)).get(product_id, 0) or 0)
        threshold = normalize_threshold(product.get('low_stock_threshold'))
        if is_stock_paused(stock, threshold):
            await query.answer(stock_pause_text(stock, threshold), show_alert=True)
            return
        quantity = max(1, min(int(quantity or 1), stock))
        context.user_data['quick_order_item'] = {
            'product_id': product.get('id'),
            'product_name': product.get('name'),
            'parent_name': product.get('parent_name'),
            'display_name': f"品类({product.get('parent_name')})口味({product.get('name')})" if product.get('parent_name') else product.get('name'),
            'quantity': quantity,
        }
        text = (
            f"✅ 已选择：品类({product.get('parent_name') or '未分类'})口味({product.get('name')}) x{quantity}\n\n"
            "请直接发送收货信息，格式如下：\n"
            "<code>北京市朝阳区xxx街道xxx号\n"
            "张三\n"
            "13800138000</code>"
        )
        await query.edit_message_text(text, parse_mode='HTML')

    async def profile_center(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """个人中心：欠款/单价说明"""
        await self.debt(update, context)
        extra = (
            "\n\n👤 <b>个人中心</b>\n"
            "• 上方为你的当前欠款\n"
            "• 点「单价查询」可看你的专属单价\n"
            "• 结账支持人民币 / USDT，人工确认入账"
        )
        await self._safe_reply(update, extra)

    async def cart_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self._is_wholesale_bot():
            # 进入选货页即表示取消此前“等待收货信息”的状态，购物车内容仍保留。
            context.user_data.pop('quick_order_item', None)
            context.user_data.pop('wholesale_cart_checkout', None)
            context.user_data.pop('wholesale_cart_quantity_input', None)
            await self.show_wholesale_cart_categories(update, context)
            return
        text = (
            "🛒 <b>购物车</b>\n\n"
            "当前支持「模板一键下单」：\n"
            "把地址/姓名/电话/商品内容按模板发过来，系统会生成确认单。\n\n"
            "单价查询可查看你的品类价格，再按模板下单。"
        )
        await self._safe_reply(update, text)

    async def express_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # show recent shipped orders for user if any
        telegram_id = str(update.effective_user.id)
        orders = (await self._api_call('get_user_orders', telegram_id)) or []
        shipped = [o for o in orders if o.get('status') == 'shipped'][:5]
        lines = ["📦 <b>快递查询</b>", ""]
        if not shipped:
            lines.append("暂无已发货订单。发货后这里会显示运单备注。")
        else:
            for o in shipped:
                note = o.get('notes') or ''
                lines.append(f"• {o.get('order_number')}  {o.get('status')}")
                if note:
                    lines.append(f"  {note}")
        lines.append("")
        lines.append("完整记录请点「下单记录」。")
        await self._safe_reply(update, chr(10).join(lines))

    async def service_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        accounts = "\n".join([f"客服账号：@{username}" for username in self._service_usernames()])
        text = (
            "☎ <b>联系客服</b>\n\n"
            f"{accounts}\n"
            "点击下方按钮即可跳转私聊。"
        )
        kb = self._contact_inline_keyboard("service")
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif update.message:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await self._safe_reply(update, text)

    async def custom_bot_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        username = self.CUSTOM_BOT_USERNAME
        text = (
            "🛠 <b>定制机器人</b>\n\n"
            f"定制开发请联系：@{username}\n"
            "点击下方按钮即可跳转私聊。"
        )
        kb = self._contact_inline_keyboard("custom_bot")
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif update.message:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await self._safe_reply(update, text)

    async def dropship_apply_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """用户申请代发/批发下单授权。"""
        user = update.effective_user
        if not user:
            await self._safe_reply(update, "无法识别 Telegram 用户，请重新发送 /start 后再申请。")
            return

        telegram_id = str(user.id)
        username = user.username or ""
        ok, resp = await self._api_call('apply_dropship_auth',
            telegram_id,
            username=username,
            telegram_name=getattr(user, "full_name", None),
            message="用户通过 Bot 菜单申请批发下单授权" if self._is_wholesale_bot() else "用户通过 Bot 菜单申请代发下单授权",
        )
        if not ok:
            err = resp.get("error") if isinstance(resp, dict) else resp
            await self._safe_reply(update, f"❌ 申请提交失败：{err or '未知错误'}")
            return

        already_approved = bool(resp.get("already_approved")) if isinstance(resp, dict) else False
        auth_name = "批发" if self._is_wholesale_bot() else "代发"
        if already_approved:
            text = (
                f"✅ <b>你已经通过{auth_name}授权</b>\n\n"
                "如需调整商品单价，请联系管理员在后台修改。"
            )
        else:
            text = (
                f"✅ <b>{auth_name}下单授权申请已提交</b>\n\n"
                "管理员会在后台看到你的申请，并为你设置商品单价。\n"
                "设置完成后，你可以通过「单价查询」查看专属单价并下单。"
            )
        await self._safe_reply(update, text)

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = (query.data or "").strip()
        if data == "noop":
            return
        if data.startswith(('quick:','qty:','cart:')) and not await self._current_order_eligibility(update):
            await query.edit_message_text('请先通过代发申请审核后下单；历史订单和账务仍可查看。')
            return
        if data.startswith("cart:"):
            if not self._is_wholesale_bot():
                await query.edit_message_text("当前机器人未启用批发购物车")
                return
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""
            if action != "input":
                context.user_data.pop('wholesale_cart_quantity_input', None)
            if action == "browse":
                await self.show_wholesale_cart_categories(update, context)
                return
            if action == "view":
                await self.show_wholesale_cart_view(update, context)
                return
            if action == "clear":
                context.user_data.pop('wholesale_cart', None)
                context.user_data.pop('wholesale_cart_checkout', None)
                await self.show_wholesale_cart_view(update, context)
                return
            if action == "checkout":
                await self.start_wholesale_cart_checkout(update, context)
                return
            if action == "input" and len(parts) == 6:
                try:
                    await self.start_wholesale_cart_quantity_input(
                        update,
                        context,
                        product_id=int(parts[2]),
                        parent_id=int(parts[3]),
                        page=int(parts[4]),
                        target=parts[5],
                    )
                except (TypeError, ValueError):
                    await query.edit_message_text("数量输入目标无效")
                return
            if action == "cat" and len(parts) == 4:
                try:
                    await self.show_wholesale_cart_category(
                        update,
                        context,
                        parent_id=int(parts[2]),
                        page=int(parts[3]),
                    )
                except (TypeError, ValueError):
                    await query.edit_message_text("型号无效")
                return
            if action == "qty" and len(parts) == 6:
                try:
                    await self.adjust_wholesale_cart_quantity(
                        update,
                        context,
                        product_id=int(parts[2]),
                        delta=int(parts[3]),
                        parent_id=int(parts[4]),
                        page=int(parts[5]),
                    )
                except (TypeError, ValueError):
                    await query.edit_message_text("数量无效")
                return
            await query.edit_message_text("购物车操作无效")
            return
        if data.startswith("cat:"):
            try:
                parts = data.split(":")
                parent_id = int(parts[1])
                page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
            except Exception:
                await query.edit_message_text("分类无效")
                return
            await self.show_category_products_page(update, context, parent_id, page=page)
            return
        if data.startswith("prod:"):
            try:
                _, product_id, parent_id, page = data.split(":")
                await self.show_product_quick_order(update, context, int(product_id), int(parent_id), int(page), 1)
            except Exception:
                await query.edit_message_text("商品无效")
            return
        if data.startswith("qty:"):
            try:
                _, product_id, quantity, parent_id, page = data.split(":")
                await self.show_product_quick_order(update, context, int(product_id), int(parent_id), int(page), int(quantity))
            except Exception:
                await query.edit_message_text("数量无效")
            return
        if data.startswith("quick:"):
            try:
                _, product_id, quantity = data.split(":")
                await self.start_quick_order(update, context, int(product_id), int(quantity))
            except Exception:
                await query.edit_message_text("下单信息无效")
            return
        if data.startswith("menu:"):
            action = data.split(":", 1)[1]
            await self._dispatch_menu_action(action, update, context)
            return
        await query.edit_message_text("未知操作")

    async def inbound_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """管理员入库帮助"""
        if not self._is_admin(update.effective_user.id):
            await self._safe_reply(update, '❌ 仅管理员可入库')
            return
        await self._safe_reply(update, INBOUND_FORMAT_EXAMPLE)

    async def _safe_reply(self, update: Update, text: str, parse_mode: str = 'HTML'):
        user_id = update.effective_user.id if update.effective_user else None
        kb = self._main_reply_keyboard(user_id)
        if update.message:
            await self._send_chunked_message(update.message.reply_text, text, parse_mode=parse_mode, reply_markup=kb)
        elif update.callback_query and update.callback_query.message:
            await self._send_chunked_message(update.callback_query.message.reply_text, text, parse_mode=parse_mode, reply_markup=kb)

    async def _dispatch_menu_action(self, action: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """统一分发菜单动作，兼容 callback/message。"""
        # Ensure update.message exists for legacy handlers
        msg = update.message
        if msg is None and update.callback_query is not None:
            msg = update.callback_query.message
            # Create a shallow wrapper via context
            context._menu_message = msg  # type: ignore

        class _UpdateProxy:
            def __init__(self, raw, message):
                self._raw = raw
                self.message = message
                self.callback_query = raw.callback_query
                self.effective_user = raw.effective_user
                self.effective_chat = raw.effective_chat
                self.update_id = raw.update_id
            def __getattr__(self, item):
                return getattr(self._raw, item)

        proxied = _UpdateProxy(update, msg)

        if action == 'order':
            await self.order_guide(proxied, context)
        elif action == 'stock':
            await self.stock(proxied, context)
        elif action == 'products':
            await self.products(proxied, context)
        elif action == 'orders':
            await self.orders(proxied, context)
        elif action in ('debt', 'profile'):
            await self.profile_center(proxied, context)
        elif action == 'cart':
            await self.cart_info(proxied, context)
        elif action == 'service':
            await self.service_info(proxied, context)
        elif action == 'custom_bot':
            await self.custom_bot_info(proxied, context)
        elif action == 'dropship':
            await self.dropship_apply_info(proxied, context)
        elif action == 'help':
            await self.help_command(proxied, context)
        elif action == 'stats':
            await self.stats_command(proxied, context)
        elif action == 'back':
            await self.start(proxied, context)
        else:
            await self._safe_reply(update, '未知功能')

    async def handle_menu_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """处理底部 ReplyKeyboard 文本按钮，命中则返回 True。"""
        text = (update.message.text or '').strip() if update.message else ''
        mapping = {
            self.BTN_ORDER: 'order',
            self.BTN_STOCK: 'stock',
            self.BTN_PRODUCTS: 'products',
            self.BTN_ORDERS: 'orders',
            self.BTN_PROFILE: 'profile',
            self.BTN_CART: 'cart',
            self.BTN_SERVICE: 'service',
            self.BTN_CUSTOM_BOT: 'custom_bot',
            self.BTN_DROPSHIP: 'dropship',
            self.BTN_WHOLESALE_APPLY: 'dropship',
            self.BTN_HELP: 'help',
            self.BTN_STATS: 'stats',
            self.BTN_BACK: 'back',
            # 兼容旧文案
            '📋 下单说明': 'order',
            '📦 查库存': 'stock',
            '🛒 商品单价': 'products',
            '🧾 我的订单': 'orders',
            '💳 我的欠款': 'profile',
            '❓ 帮助': 'help',
        }
        action = mapping.get(text)
        if not action:
            return False
        await self._dispatch_menu_action(action, update, context)
        return True

    async def handle_order_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self._is_group_chat(update) or ':' in str(update.callback_query.data or ''):
            query=update.callback_query
            parts=str(query.data or '').split(':',1)
            if len(parts)!=2:
                await query.answer('请使用对应订单的确认按钮',show_alert=True)
                return
            account=await self._api_call('get_telegram_account',telegram_id=str(update.effective_user.id))
            actor_authorized=isinstance(account,dict) and account.get('authorized')
            draft=(await self._api_call('get_group_draft',update.effective_chat.id,parts[1],sender_id=str(update.effective_user.id),include_flavor_expired=True)) if actor_authorized else None
            proxy=False
            from proxy_confirmation import INITIAL_IDENTITIES
            if not isinstance(draft,dict) and parts[0]=='order_confirm' and str(update.effective_user.id) in INITIAL_IDENTITIES and not self._is_wholesale_bot():
                draft=await self._api_call('get_proxy_confirmation_draft',update.effective_chat.id,parts[1],sender_id=str(update.effective_user.id))
                if isinstance(draft,dict) and isinstance(draft.get('_proxy_account'),dict):
                    proxy=True;account=draft['_proxy_account']
            if not actor_authorized and not proxy:
                await query.answer('下单资格或代确认权限无效',show_alert=True)
                return
            if isinstance(draft,dict) and draft.get('status') in ('flavor_expired','confirmation_expired'):
                from flavor_draft_expiry import MESSAGE
                await query.answer(MESSAGE,show_alert=True);return
            if not isinstance(draft,dict):
                await query.answer('订单已处理或已过期',show_alert=True)
                return
            from telegram_order_context import AccountUpdate,DraftContext
            scoped=DraftContext(context,draft)
            if proxy:scoped.user_data['_proxy_confirmation']=dict(actor_id=str(update.effective_user.id),draft_token=draft['token'])
            try:
                await self._handle_order_callback(AccountUpdate(update,account,draft),scoped)
            finally:
                completed=not scoped.user_data.get('pending_order')
                if not proxy:
                    saved,_=await self._api_call('save_group_draft',update.effective_chat.id,draft['token'],scoped.user_data,completed,sender_id=str(update.effective_user.id))
                    if not saved:raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')
            return
        await self._handle_order_callback(update,context)

    async def _handle_order_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理下单确认/取消"""
        query = update.callback_query
        await query.answer()
        data = str(query.data or '').split(':',1)[0]

        if not context.user_data.get('pending_order'):
            await query.answer('本次确认已结束或过期，未重复执行；订单结果请查看最终回执。')
            return

        if data == "order_cancel":
            source = context.user_data.pop('pending_order_source', '')
            context.user_data.pop('pending_order', None)
            if source == 'wholesale_cart':
                await query.edit_message_text("❌ 已取消本次结算，购物车商品已保留",reply_markup=None)
            else:
                await query.edit_message_text("❌ 本次订单已取消",reply_markup=None)
            return

        if data != "order_confirm":
            return

        if not await self._current_order_eligibility(update):
            context.user_data.pop('pending_order',None)
            context.user_data.pop('pending_order_source',None)
            await query.edit_message_text('下单资格未通过或已停用，本次未创建订单或扣减库存。',reply_markup=None)
            return

        preview = context.user_data.get('pending_order')
        if not preview:
            await query.edit_message_text("⚠️ 订单已过期，请重新发送订单信息")
            return

        if preview.get('_cross_warehouse_pending'):
            confirmed=dict(preview,_cross_warehouse_confirmed=True,_receipt_message=query.message)
            await self._record_automatic_order(update,context,confirmed)
            return

        # 防止重复点击
        source = context.user_data.pop('pending_order_source', '')
        context.user_data.pop('pending_order', None)
        # Preserve the confirmation message until its final outcome is known.

        items_payload = [{
            'product_id': item.get('product_id'),
            'product_name': item['product_name'],
            'parent_name': item.get('parent_name'),
            'quantity': item['quantity'],
            'warehouse_name': item['warehouse_name'],
        } for item in preview['items']]

        recipient_info = {
            'recipient_name': preview['recipient_name'],
            'recipient_phone': preview['recipient_phone'],
            'recipient_address': preview['recipient_address'],
        }

        success, order = await self._api_call(
            'create_order',
            telegram_id=preview['telegram_id'],
            items=items_payload,
            recipient_info=recipient_info,
            source_info={key:preview.get(key) for key in ('source_chat_id','source_message_id','source_chat_type','source_chat_title')},
            expected_quote={key: preview[key] for key in (
                'items', 'total_amount', 'total_shipping_fee', 'grand_total')},
            **({'proxy_info':context.user_data['_proxy_confirmation']} if context.user_data.get('_proxy_confirmation') else {}),
        )

        if not success:
            err = order.get('error') if isinstance(order, dict) else order
            if str(err).startswith('❌ 录单失败'):
                for key in ('pending_order','pending_order_source','pending_recipient_order','pending_ai_flavor','pending_manual_review'):
                    context.user_data.pop(key,None)
                await query.edit_message_text(str(err),reply_markup=None)
                return
            if isinstance(err, dict) and err.get('code') == 'RECONFIRM_REQUIRED':
                revised = dict(preview)
                revised.update(err['quote'])
                revised['warnings'] = ['最终仓库或价格发生变化，请重新确认；尚未创建订单或扣减库存。']
                await query.edit_message_text('⚠️ 最终价格发生变化，正在重新展示订单，请重新确认。')
                await self._send_order_preview(update, context, revised, source=source)
                return
            await query.edit_message_text(f"❌ 下单失败: {err}")
            return

        if source == 'wholesale_cart':
            context.user_data.pop('wholesale_cart', None)
            context.user_data.pop('wholesale_cart_checkout', None)

        # 成交回执只读取持久化订单，不能回退到确认前报价。
        if not isinstance(order, dict) or not order.get('items') or any(
            key not in order for key in ('total_amount', 'total_shipping_fee', 'grand_total')
        ):
            await query.edit_message_text('⚠️ 订单已提交，但成交明细返回不完整，请在订单记录中核实。请勿重复提交。')
            return
        remaining_stock = order.get('remaining_stock') or []
        remain_map = {
            (r.get('product_id'), r.get('warehouse_name')): r.get('remaining_quantity')
            for r in remaining_stock
        }
        confirm_items = []
        for item in order['items']:
            product_name = item.get('product_name') or ''
            parent_name = item.get('parent_name') or item.get('category_name')
            display_name = item.get('display_name') or (
                f"品类({parent_name})口味({product_name})"
                if parent_name else product_name
            )
            confirm_items.append({
                'product_name': product_name,
                'parent_name': parent_name,
                'display_name': display_name,
                'quantity': item['quantity'],
                'unit_price': item['unit_price'],
                'warehouse_name': item['warehouse_name'],
                'remaining_quantity': item.get('remaining_quantity', remain_map.get((item.get('product_id'), item['warehouse_name'])))
            })

        order_info = {
            'items': confirm_items,
            'total_amount': order['total_amount'],
            'total_shipping_fee': order['total_shipping_fee'],
            'grand_total': order['grand_total'],
            'recipient_name': order.get('recipient_name'),
            'recipient_phone': order.get('recipient_phone'),
            'recipient_address': order.get('recipient_address'),
            'order_number': order.get('order_number'),
            'order_numbers': order.get('order_numbers'),
            'is_split': order.get('is_split', False),
            'split_count': order.get('split_count', 1),
            'created_at': order.get('created_at'),
            'user_balance': order.get('user_balance'),
            'user_debt': order.get('user_debt'),
            'daily_summary': order.get('daily_summary'),
            'payment_status': order.get('payment_status', 'unpaid'),
            'remaining_stock': remaining_stock,
        }

        # Both group and private successful confirmations use committed facts
        # and the same final template, without a second success message.
        order_info.update(_automatic_receipt=True,_receipt_message=query.message,_recipient_generated=preview.get('_recipient_generated') is True)
        try:
            from learning_pipeline import record_committed
            await asyncio.to_thread(record_committed,preview,order)
        except Exception as exc:
            logger.warning('COMMITTED_LEARNING_DEFERRED type=%s',type(exc).__name__)
        await self._send_order_confirmation(context.bot, query.message.chat_id, order_info)


    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/cancel 订单号  提交取消申请，等待后台审核"""
        await update.message.reply_text('订单已录入，如需取消订单或修改收件信息/商品信息，请联系客服处理。')
        return
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text(
                "用法: /cancel 订单号\n例如: /cancel ORD20260101120000ab12cd\n或: /cancel #ORD..."
            )
            return

        order_number = args[0].strip().lstrip('#')
        reason = ' '.join(args[1:]).strip() or '用户主动取消'
        telegram_id = str(update.effective_user.id)

        # Keep cancellation off the polling event loop while preserving the
        # concrete client call for deployment instrumentation.
        def submit_cancellation_request():
            return api_client.request_order_cancellation(
                order_number=order_number,
                telegram_id=telegram_id,
                reason=reason,
            )

        ok, result = await asyncio.to_thread(submit_cancellation_request)
        if not ok:
            await update.message.reply_text(f"❌ 取消失败: {result}")
            return

        data = result.get('request', {}) if isinstance(result, dict) else {}
        if not isinstance(data, dict):
            data = {}
        number = data.get('order_number', order_number)
        duplicate = bool(data.get('duplicate'))
        prefix = "\u2139\ufe0f" if duplicate else "\ud83d\udce9"
        suffix = "\n\u8be5\u8ba2\u5355\u5df2\u6709\u5f85\u5ba1\u6838\u7533\u8bf7\uff0c\u8bf7\u52ff\u91cd\u590d\u63d0\u4ea4" if duplicate else ""
        await update.message.reply_text(
            f"{prefix} \u8ba2\u5355 #{number} \u7684\u53d6\u6d88\u7533\u8bf7\u5df2\u63d0\u4ea4\u3002\n"
            f"\u8bf7\u7b49\u5f85\u540e\u53f0\u5ba1\u6838\uff0c\u5ba1\u6838\u901a\u8fc7\u524d\u8ba2\u5355\u4ecd\u7136\u6709\u6548\uff0c\u4e0d\u4f1a\u9000\u6b3e\u6216\u56de\u5e93\u3002{suffix}"
        )
        return


    async def order_guide(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """下单说明"""
        text = self._wholesale_order_guide_text() if self._is_wholesale_bot() else ORDER_GUIDE
        await self._safe_reply(update, text)

    
    async def debt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查询当前欠款"""
        telegram_id = str(update.effective_user.id)
        success, user = (await self._api_call('create_or_get_user', 
            telegram_id,
            update.effective_user.username,
            getattr(update.effective_user, 'full_name', None),
        ))
        if not success or not user:
            await update.message.reply_text('❌ 无法获取用户信息')
            return
        debt_val = user.get('debt')
        balance = user.get('balance', 0) or 0
        if debt_val is None:
            detail = (await self._api_call('get_user_prices', user['id']))
            debt_val = detail.get('debt', 0)
            balance = detail.get('balance', balance)
        msg = (
            "💳 <b>账户信息</b>" + chr(10) +
            f"• 当前欠款: ¥{float(debt_val or 0):.2f}" + chr(10) +
            f"• 预存余额: ¥{float(balance or 0):.2f}" + chr(10) +
            "• 结账方式: 人工收账（人民币/USDT）" + chr(10) +
            "• 不强制每单一结，下单后累计欠款"
        )
        await update.message.reply_text(msg, parse_mode='HTML')


    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """管理员查看今日统计"""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text('❌ 仅管理员可查看统计')
            return
        day = (getattr(context, "args", None) or [None])[0]
        data = await self._api_call('daily_stats',day)
        if not data:
            await update.message.reply_text('❌ 暂无统计数据')
            return
        breakdown = await self._api_call('sales_breakdown',"day", data.get('day'))
        breakdown_lines = []
        for item in (breakdown.get('items', []) if breakdown else [])[:20]:
            category = html.escape(str(item.get('parent_name') or '默认分类'))
            flavor = html.escape(str(item.get('product_name') or '-'))
            breakdown_lines.append(
                f"• {category} / {flavor}: {int(item.get('quantity', 0) or 0)} 件"
            )
        breakdown_text = ""
        if breakdown_lines:
            breakdown_text = chr(10) + chr(10) + "📈 <b>品类 / 口味销售数量</b>" + chr(10) + chr(10).join(breakdown_lines)
        msg = (
            f"📊 <b>{data.get('day')} 数据统计</b>" + chr(10) + chr(10) +
            f"🧾 新订单: {data.get('orders_created', 0)} 单" + chr(10) +
            f"💰 下单金额: ¥{float(data.get('orders_amount', 0) or 0):.2f}" + chr(10) +
            f"🚚 发货订单: {data.get('orders_shipped', 0)} 单" + chr(10) +
            f"📦 发货金额: ¥{float(data.get('shipped_amount', 0) or 0):.2f}" + chr(10) +
            f"📥 补货条数: {data.get('inbound_count', 0)} / 数量 {data.get('inbound_qty', 0)}" + chr(10) +
            f"📤 出库条数: {data.get('outbound_count', 0)} / 数量 {data.get('outbound_qty', 0)}" + chr(10) +
            f"💵 今日收款: ¥{float(data.get('payments_amount', 0) or 0):.2f}" + chr(10) +
            f"⚠️ 当前总欠款: ¥{float(data.get('total_debt', 0) or 0):.2f}" +
            breakdown_text
        )
        await update.message.reply_text(msg, parse_mode='HTML')


    async def _current_order_eligibility(self, update):
        sender=getattr(update,'effective_user',None)
        if sender is None:return False
        account=await self._api_call('get_telegram_account',telegram_id=str(sender.id))
        return isinstance(account,dict) and bool(account.get('authorized'))

    from order_trace import traced as _traced

    @_traced
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.message or not update.message.text:
            return
        sender=getattr(update,'effective_user',None)
        if sender is None or getattr(sender,'is_bot',False):
            return
        # Menu operations are not order supplements, even with many drafts.
        if await self.handle_menu_text(update,context):
            return
        text=update.message.text.strip()
        from order_structure import is_order_receipt
        if is_order_receipt(text):
            await update.message.reply_text('ℹ️ 这是已有订单的成功回执，不作为新订单处理；本次未重复录单、未扣库存。请发送新的完整订单原文。')
            return
        if self._is_non_order_intent(text):
            return
        # Contact-only input explicitly requested by a menu checkout is not
        # an order-draft supplement. Complete new merchandise orders still
        # get their own durable draft below.
        if (not self._is_group_chat(update) and
                (context.user_data.get('quick_order_item') or context.user_data.get('wholesale_cart_checkout'))):
            from contact_fields import with_generated_name
            contact = with_generated_name(parse_contact(text))
            if not any(diagnose_contact(contact)[key] for key in ('missing', 'invalid')):
                parsed = OrderParser.extract_order_info(text)
                if not parsed or not parsed.get('items'):
                    account = await self._api_call('get_telegram_account', telegram_id=str(sender.id))
                    if not isinstance(account, dict) or not account.get('authorized'):
                        await update.message.reply_text('⚠️ 当前暂无下单权限\n请先通过代发申请审核后下单；历史订单和账务仍可查看。')
                        return
                    if context.user_data.get('wholesale_cart_checkout'):
                        await self._handle_wholesale_cart_delivery(update, context, text)
                    else:
                        await self._handle_quick_order_delivery(update, context, text)
                    return
        from order_trace import stage
        # An explicit detail-only reply belongs to its owned address draft,
        # even when door/building numbers resemble a new order quantity.
        if (getattr(update.message,'reply_to_message',None)
                and re.match(r'^\s*(?:详细地址|补充地址|地址)\s*[:：]',text)
                and await self._resume_address_supplement(update,context,text)):
            return
        with stage('ORDER_CANDIDATE'):
            new_order=self._looks_like_order_candidate(text)
            if not new_order:new_order=await self._bundle_candidate(text)
        search_chat=context.user_data.get('account_search_chat_id')
        if not new_order and context.user_data.get('account_search_pending') and (
                search_chat==str(update.effective_chat.id) or
                (search_chat is None and not self._is_group_chat(update))):
            await self._account_search_result(update,context,text)
            return
        if not new_order and await self._resume_address_supplement(update,context,text):
            return
        if not new_order:
            # Only explicit menu workflows may consume free text. Order
            # drafts advance through their own token-scoped inline callbacks.
            if (not self._is_group_chat(update) and
                    context.user_data.get('wholesale_cart_quantity_input') and re.fullmatch(r'\d+',text)):
                await self._handle_wholesale_cart_quantity_input(update,context,text)
            return
        if (not self._is_group_chat(update) and not getattr(context,'_rainbow_queue_resume',False)
                and not any(context.user_data.get(key) for key in (
                    'pending_recipient_order','pending_order','pending_ai_flavor','pending_manual_review'))):
            # Legacy direct entry remains available for menu checkout/internal
            # callers. Durable receipt workers and all existing drafts use the
            # token-scoped route below, never sender-global pending state.
            account=await self._api_call('get_telegram_account',telegram_id=str(sender.id))
            if not isinstance(account,dict) or not account.get('authorized'):
                await update.message.reply_text('⚠️ 当前暂无下单权限\n请先通过代发申请审核后下单；历史订单和账务仍可查看。')
                return
            await self._handle_order_message(update,context)
            return
        message_id=getattr(update.message,'message_id',None)
        logger.info('RAW_GROUP_UPDATE_RECEIVED chat=%s message=%s sender=%s',update.effective_chat.id,message_id,sender.id)
        account=await self._api_call('get_telegram_account',telegram_id=str(sender.id))
        logger.info('CUSTOMER_LOOKUP sender=%s found=%s ORDER_PERMISSION=%s',sender.id,bool(isinstance(account,dict) and account.get('user_id')),bool(isinstance(account,dict) and account.get('authorized')))
        if not isinstance(account,dict):
            if getattr(context,'_rainbow_queue_resume',False):raise RuntimeError('ACCOUNT_LOOKUP_UNAVAILABLE')
            return
        if not account.get('authorized'):
            if self._looks_like_order_candidate(update.message.text or ''):
                await update.message.reply_text('⚠️ 当前暂无下单权限\n请先通过代发申请审核后下单；历史订单和账务仍可查看。')
            return
        logger.info('ORDER_CANDIDATE chat=%s message=%s candidate=%s',update.effective_chat.id,message_id,new_order)
        ok,result=await self._api_call('open_group_draft',update.effective_chat.id,
            update.message.message_id,sender.id,new_order=new_order,
            reply_message_id=None,
            **({'resume':True} if getattr(context,'_rainbow_queue_resume',False) else {}))
        if not ok:
            if (getattr(context,'_rainbow_queue_resume',False) and
                    result in ('请求超时','无法连接到 API 服务器，请检查是否已启动')):
                # Do not turn an unconfirmed draft-open write into a final
                # rejection/empty confirmation. Durable retry resumes the
                # exact source claim; it never invents a new order message.
                raise RuntimeError('ORDER_DRAFT_OPEN_UNCERTAIN')
            await update.message.reply_text('请回复对应订单原消息补充信息：'+str(result))
            return
        draft=result.get('draft') if isinstance(result,dict) else None
        if not draft:return
        from telegram_order_context import AccountUpdate,DraftContext
        scoped=DraftContext(context,draft)
        scoped_update=AccountUpdate(update,account,draft)
        await self._start_agent_group_receipt(scoped_update,scoped)
        if scoped.user_data.get('_group_receipt_ack_attempted'):
            saved,_=await self._api_call('save_group_draft',update.effective_chat.id,
                draft['token'],scoped.user_data,False,sender_id=str(sender.id))
            if not saved:raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')
        try:
            logger.info('PARSER chat=%s message=%s account=%s draft=%s',update.effective_chat.id,update.message.message_id,account.get('user_id'),draft['token'])
            await self._handle_order_message(scoped_update,scoped)
        except asyncio.CancelledError:
            # Shutdown during AI parsing is not a business rejection. Preserve
            # the pending draft so the persisted message can resume after its
            # lease expires, rather than marking an empty draft completed.
            scoped._rainbow_processing_failed=True
            context._rainbow_processing_failed=True
            raise
        finally:
            failed=bool(getattr(scoped,'_rainbow_processing_failed',False))
            if failed:context._rainbow_processing_failed=True
            completed=not failed and not any(scoped.user_data.get(key) for key in ('pending_recipient_order','pending_order','pending_manual_review','pending_ai_flavor'))
            saved,_=await self._api_call('save_group_draft',update.effective_chat.id,draft['token'],scoped.user_data,completed,sender_id=str(sender.id))
            if not saved:
                logger.error('群草稿保存失败 chat=%s message=%s',update.effective_chat.id,update.message.message_id)
                if getattr(context,'_rainbow_queue_resume',False):
                    raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')

    async def _handle_order_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理用户消息：菜单按钮与下单预览确认"""
        # Edited/channel updates may match filters.TEXT while carrying no
        # ``update.message``. Ignore them instead of crashing the worker.
        if not update or not update.message:
            return
        message_text = (update.message.text or "").strip()
        if not message_text:
            return
        from order_structure import is_order_receipt
        if is_order_receipt(message_text):
            await update.message.reply_text('ℹ️ 这是已有订单的成功回执，不作为新订单处理；本次未重复录单、未扣库存。请发送新的完整订单原文。')
            return
        from order_structure import structure_order_text
        structure = await light_recognition_work(structure_order_text,message_text)
        if structure.customer_order_ref:
            logger.info('ORDER_REF_REMOVED')
        if structure.customer_warehouse_notes:
            logger.info('WAREHOUSE_NOTE_REMOVED count=%s', len(structure.customer_warehouse_notes))
        if structure.customer_amount_notes:
            logger.info('AMOUNT_NOTE_REMOVED count=%s', len(structure.customer_amount_notes))

        # 底部功能按钮
        if await self.handle_menu_text(update, context):
            return

        if self._is_non_order_intent(message_text):
            return

        # Group conversations are noisy. Do not call DeepSeek, queue manual
        # review, or send an error for ordinary chat text. Pending delivery
        # replies remain eligible because they are part of an existing order
        # flow and are authorization-checked below.
        if (self._is_group_chat(update) and not self._looks_like_order_candidate(message_text)
                and not await self._bundle_candidate(message_text)):
            logger.debug('忽略群内非订单消息 chat=%s user=%s',
                getattr(getattr(update, 'effective_chat', None), 'id', None),
                getattr(getattr(update, 'effective_user', None), 'id', None))
            return

        # 配置代发群中的订单相关文本先做授权校验。未通过、审核中、被拒绝或
        # 授权查询失败时，不进入任何订单状态处理，也不会创建订单。
        if not await self._ensure_agent_group_authorized(update):
            return

        # An explicitly numbered new message with its own product lines is
        # not a contact supplement to a previous private draft. Group drafts
        # already isolate source message tokens in the durable draft service.
        if context.user_data.get('pending_recipient_order') and structure.customer_order_ref:
            fresh = OrderParser.extract_order_info(message_text)
            if fresh and fresh.get('items'):
                context.user_data.pop('pending_recipient_order', None)
                logger.info('NEW_ORDER_CONTEXT')

        if context.user_data.get('wholesale_cart_quantity_input'):
            if await self._handle_wholesale_cart_quantity_input(update, context, message_text):
                return

        if context.user_data.get('wholesale_cart_checkout'):
            if await self._handle_wholesale_cart_delivery(update, context, message_text):
                return

        if context.user_data.get('quick_order_item'):
            if await self._handle_quick_order_delivery(update, context, message_text):
                return

        # 收件人缺失：上一轮已经问过客户了，他回复的「姓名」先接到这里补全再下单。
        if context.user_data.get('pending_recipient_order'):
            try:
                if await self._handle_pending_recipient_reply(update, context, message_text):
                    return
            except Exception:
                logger.exception('补充收件信息失败')
                await update.message.reply_text('❌ 发生错误，请稍后重试。收件草稿已保留。')
                return

        if await self._handle_bundle_text(update,context,message_text):
            return
        diagnostic_scope = diagnostic_request('ORDER_PARSE', message_text)
        diagnostic_scope.__enter__()
        from failure_analysis import REJECTION_REPLIES
        rejection_reply_token=REJECTION_REPLIES.set([])
        ai_attempted = False
        try:
            from order_trace import stage
            with stage('LOCAL_PARSER'):
                # Catalog and reviewed category aliases are complete tokens,
                # not weak flavor substrings. Original evidence stays intact.
                from category_evidence import prepare
                from rule_dictionary import runtime_store
                alias_products, _alias_warehouses, _alias_user=await self._visible_catalog_for_user_async(update)
                def parse_local_order():
                    with stage('CATEGORY_PREPARE'):
                        prepared_text=prepare(message_text,alias_products,runtime_store()['aliases'])
                    principal=getattr(getattr(update,'effective_user',None),'id',None)
                    from learning_knowledge import principal_scope
                    with principal_scope(principal):
                        # Metadata and conservation checks must own the real
                        # customer source, never a generated canonical string.
                        parsed=OrderParser.extract_order_info(message_text,alias_products)
                    if isinstance(parsed,dict):
                        from brand_aliases import matched_reviewed_aliases
                        with stage('REVIEWED_ALIAS_MATCH'):
                            parsed['reviewed_knowledge_hits']=(parsed.get('reviewed_knowledge_hits') or [])+matched_reviewed_aliases(message_text,alias_products)
                    return prepared_text,parsed
                local_text,order_info=await recognition_work(parse_local_order)
                if re.search(r'(?:随机|随便)\s*[\d一二两三四五六七八九十]+\s*(?:支|根|个)',local_text):
                    from random_order import expand
                    stock_map=await self._product_stock_map_async(_alias_warehouses, alias_products)
                    # Explicit random authorization does not override the
                    # existing sellability/reserve policy.
                    stock_map={p['id']: (0 if is_stock_paused(int(stock_map.get(p['id'],0) or 0),
                        normalize_threshold(p.get('low_stock_threshold'))) else stock_map.get(p['id'],0))
                        for p in alias_products}
                    try:
                        random_items=await asyncio.to_thread(expand,message_text,alias_products,stock_map)
                    except ValueError as error:
                        await update.message.reply_text('❌ '+str(error))
                        return
                    if random_items:
                        order_info=await asyncio.to_thread(OrderParser._attach_parse_metadata,
                            message_text,dict(order_info or {},items=random_items),alias_products)
                        order_info['requires_item_confirmation']=False
            diagnostic_event('LOCAL_RESULT', 'SUCCESS' if isinstance(order_info, dict) and OrderParser.validate_order(order_info)[0] else 'REJECTED')
            local_order = order_info
            # Freeze the genuine local stage before contact/AI recovery mutates it.
            import copy
            local_evidence = copy.deepcopy(order_info) if isinstance(order_info, dict) else None
            if not self._is_wholesale_bot():
                record_contact_evidence('ORDER_LOCAL',local_evidence,message_text)
            learning_ai_state = {'order': 'not_called', 'contact': 'not_called'}
            if isinstance(order_info, dict):
                order_info['_learning_local_parser_result'] = local_evidence
                order_info['_learning_ai_called'] = False
                order_info['_learning_ai_state'] = learning_ai_state
            # Repair only unresolved contact fields before asking Order AI to
            # reinterpret already deterministic merchandise.
            if isinstance(local_order,dict) and not self._is_wholesale_bot():
                # Category preparation may split a goods line into a standalone
                # heading. Ground the initial recipient against the customer's
                # untouched text before treating it as a locked contact field.
                if local_order.get('recipient') and not name_has_evidence(local_order['recipient'],message_text):
                    local_order=dict(local_order,recipient=None)
                local_order['raw_text']=message_text
                from learning_evidence import CONTACT_CAPTURE
                capture_token = CONTACT_CAPTURE.set(None)
                with stage('EARLY_CONTACT_RECOVERY'):
                    contact_fields=await self._contact_fields_async(message_text,local_order,products=alias_products)
                contact_evidence = CONTACT_CAPTURE.get() or {}
                CONTACT_CAPTURE.reset(capture_token)
                local_order=merge_contact(local_order,contact_fields)
                local_order['_learning_local_parser_result'] = local_evidence
                local_order['_learning_ai_called'] = bool(contact_evidence.get('called'))
                learning_ai_state['contact'] = contact_evidence.get('status', 'failed' if contact_evidence.get('called') else 'not_called')
                local_order['_learning_ai_state'] = learning_ai_state
                if contact_evidence.get('snapshot'):
                    local_order['_learning_deepseek_analysis'] = contact_evidence['snapshot']
                if contact_fields.get('_name_absent_confirmed'):
                    local_order['_name_absent_confirmed']=True
                order_info=local_order
            ai_config = self._ai_runtime_config()
            # A unique scoped typo or ordered abbreviation requires explicit
            # customer confirm/cancel for this order, never global approval.
            from typo_confirmation import local_result as local_typo_result
            typo=await light_recognition_work(local_typo_result,local_order,message_text,alias_products)
            if typo:
                typo_result, choices = typo
                context.user_data['pending_ai_flavor']={
                    'result':typo_result,'raw_text':message_text,'confirmed':[],
                    'pending_queue':choices,'resolved':{},'contact':{k:local_order[k] for k in FIELDS if k in local_order},
                    '_learning_meta':{'_learning_local_parser_result':local_evidence,'_learning_ai_called':False,
                                      '_learning_ai_state':learning_ai_state},'_local_typo_confirmation':True}
                diagnostic_event('FINAL_RESULT','AWAITING_VALIDATION',['PRODUCT_NOT_FOUND'])
                await self._send_ai_flavor_choice(update,context,choices[0])
                return
            # Reject a source-proven primary over-limit before creating name
            # or contact waiting state. Unresolved catalog items stay governed
            # by their normal identification/candidate checks.
            if isinstance(local_order,dict) and not local_order.get('bundle_requests'):
                primary=[]
                category=self._resolve_order_category_context(local_order,alias_products)
                for reference,quantity in self._order_catalog_items(local_order,alias_products):
                    product,_=self._resolve_product_for_order(reference,alias_products,category_context=category)
                    if product:primary.append(dict(product_id=product['id'],quantity=quantity))
                from product_order_limits import violations,rejection
                exceeded=violations(primary,alias_products)
                if exceeded:
                    self._retire_noninteractive_failure(context)
                    await update.message.reply_text(rejection(exceeded))
                    return
            ai_rejection = []
            ai_detail = {}
            ai_result = None

            # 普通下单机器人：先把原文和实时后台商品目录交给 DeepSeek。
            # AI 只负责抽取，后续唯一商品匹配、库存、价格、运费和下单仍由本地完成。
            if ai_config['enabled'] and ai_config['order_enabled'] and not self._is_wholesale_bot():
                try:
                    # Reuse this message's authorized catalog snapshot. Final
                    # quote/record endpoints still recheck live stock/price.
                    products, _visible_warehouses, _visible_user = alias_products, _alias_warehouses, _alias_user
                    # AI 这里只做文本抽取；若仓库接口暂不可用或测试 fixture 没有绑定仓，
                    # 保留原始目录供解析，最终预览/服务端仍会执行严格仓库权限校验。
                    if not products:
                        products = await self._api_call('get_products') or []
                    if not products:
                        raise RuntimeError('后台商品目录为空')
                    catalog = self._build_deepseek_catalog(products)
                    if not catalog:
                        raise RuntimeError('后台商品目录没有可识别口味')
                    local_valid = isinstance(local_order, dict) and OrderParser.validate_order(local_order)[0]
                    with stage('ALIAS_AND_CATALOG'):
                        def trusted_local_order():
                            items=self._order_catalog_items(local_order,products) if local_valid else []
                            category=self._resolve_order_category_context(local_order,products) if local_valid else None
                            trusted=local_valid and bool(items) and all(
                                self._resolve_product_for_order(name,products,category_context=category)[0]
                                for name,quantity in items)
                            return category,trusted
                        local_context,local_trusted=await asyncio.to_thread(trusted_local_order)
                    if local_trusted:
                        from learning_knowledge import requires_reviewed_grammar
                        if requires_reviewed_grammar(message_text,products,getattr(update.effective_user,'id',None)):
                            local_trusted=False
                    if local_trusted:
                        try:
                            from local_fulfillment import attach
                            local_order=await asyncio.to_thread(attach,local_order,products,lambda name:self._resolve_product_for_order(
                                name,products,category_context=local_context),defer_backup=True)
                        except ValueError:
                            # An unresolved backup needs the same catalog-bound
                            # AI path; it must not be guessed or counted as extra.
                            local_trusted=False
                    if local_trusted and local_order.get('address'):
                        try:
                            from address_source_guard import validate_explicit_region
                            compact_address=''.join(str(local_order['address']).split()).casefold()
                            identities={str(p.get(key) or '').strip().casefold()
                                for p in products for key in ('name','parent_name') if str(p.get(key) or '').strip()}
                            from rule_dictionary import runtime_store
                            identities.update(str(row.get('alias') or '').strip().casefold()
                                for row in runtime_store().get('aliases',[])
                                if row.get('status')=='active' and str(row.get('alias') or '').strip())
                            evidence_lines=list(str(local_text or '').splitlines())+list(str(message_text or '').splitlines())
                            polluted_line=any(
                                ''.join(line.split()).casefold() in compact_address
                                and re.search(r'\d+',line)
                                and any(token in line.casefold() for token in identities)
                                for line in evidence_lines
                                if line.strip())
                            if polluted_line:
                                raise ValueError('LOCAL_CONTACT_CONTAINS_MERCHANDISE')
                            validate_explicit_region(message_text,local_order['address'],products)
                        except ValueError:
                            # A catalog-complete local parse is not trusted when
                            # its contact split absorbed merchandise.  Continue
                            # to the normal AI+source validator; never preview
                            # or persist the polluted address.
                            local_trusted=False
                    if local_trusted:
                        await _record_learning_usage_noncritical(
                            hits=local_order.get('reviewed_knowledge_hits') or [],ai_called=False)
                        diagnostic_event('LOCAL_RESULT', 'SUCCESS')
                        diagnostic_event('AI_REQUEST', 'NOT_CALLED')
                        diagnostic_event('PROVIDER_RESULT', 'NOT_CALLED')
                        diagnostic_event('NORMALIZATION_RESULT', 'SKIPPED')
                        diagnostic_event('DETERMINISTIC_VALIDATION', 'SUCCESS')
                        preview = await self._build_order_preview(update, local_order)
                        diagnostic_event('FINAL_RESULT', 'LOCAL_ONLY' if preview else 'REJECTED',
                                         [] if preview else ['BUSINESS_REJECTION'], local_only=True)
                        if preview:
                            await self._send_order_preview(update, context, preview)
                        return
                    ai_attempted = True
                    learning_ai_state['order'] = 'failed'
                    if isinstance(local_order, dict):
                        local_order['_learning_ai_called'] = True
                    await _record_learning_usage_noncritical(
                        hits=(local_order or {}).get('reviewed_knowledge_hits') or [],ai_called=True)
                    from order_trace import stage
                    from ai_transport import run_provider_worker
                    with stage('ORDER_AI'):
                        ai_result = await run_provider_worker(
                            parse_order_with_deepseek, message_text, catalog,
                        )
                except Exception as ai_error:
                    if isinstance(ai_error, TelegramError):
                        # Local-first preview/group recording can already have
                        # completed business work. Never fall through and run it
                        # again merely because the subsequent send failed.
                        raise
                    # 仅网络、超时或接口异常时回退本地解析，保持机器人可用。
                    logger.warning('AI order request failed; local fallback type=%s', type(ai_error).__name__)
                else:
                    provider_evidence = copy.deepcopy(ai_result)
                    record_contact_evidence('ORDER_PROVIDER',provider_evidence,message_text)
                    learning_ai_state['order'] = 'returned'
                    from source_evidence import restore
                    ai_result=restore(ai_result,message_text)
                    from ai_flavor_decision import proposals, prepare_unique_candidates
                    from ai_catalog_candidates import refine_async
                    with stage('AI_CANDIDATE'):
                        ai_result=await refine_async(ai_result,message_text,products)
                    ai_result=prepare_unique_candidates(ai_result,message_text,products)
                    choices=proposals(ai_result,message_text,products)
                    if choices:
                        ai_rejection = [str(c.get('category') or '') + ' / ' + str(c.get('raw_flavor') or '')
                                        + ' 无法唯一确认' for c in choices]
                        diagnostic_reject(['ALIAS_CONFLICT'])
                        self._retire_noninteractive_failure(context)
                        context.user_data.pop('pending_ai_flavor',None)
                        await update.message.reply_text('❌ 本次未录单：商品叫法无法唯一确认。\n'
                            + '\n'.join(ai_rejection[:10])
                            + '\n管理员可在后台审核叫法；通过后用于后续新订单，本单不会自动重提。')
                        return
                    with stage('VALIDATOR'):
                        order_info, ai_rejection, ai_detail = await recognition_work(self._validate_ai_order_against_catalog,
                            ai_result, message_text, products,
                        )
                    if order_info:
                        learning_ai_state['order'] = 'validated'
                        order_info['_learning_deepseek_analysis']=provider_evidence
                        order_info['_learning_ai_called']=True
                        order_info['_learning_local_parser_result']=local_evidence
                        order_info['_learning_ai_state']=learning_ai_state
                    elif ai_rejection:
                        learning_ai_state['order'] = 'rejected'

            # AI 已返回结果但未通过安全校验时，不能静默改用本地解析器绕过门槛。
            if ai_rejection:
                from order_trace import mark_error
                mark_error('VALIDATOR')
                from parser_diagnostics import log_rejection
                log_rejection(message_text,local_order,ai_result,
                    (ai_result.get('missing') or []) if isinstance(ai_result,dict) else [],'AI_VALIDATION_REJECTED')
                # 按失败原因分流，不要把技术性错误原样丢给客户。

                # 1) 售后 / 补发单：转客服，客户只需知道已受理
                if (ai_detail or {}).get('is_aftersale'):
                    pushed = await self._push_aftersale_to_service(
                        update, context, message_text, ai_result
                    )
                    tail = (
                        '客服会尽快联系你，不用重复发单 🙏' if pushed
                        else '麻烦直接联系客服处理，不要走下单流程 🙏'
                    )
                    await update.message.reply_text(
                        '🔧 这单看起来是售后 / 补发，' + ('已经转给客服了。\n' if pushed else '')
                        + tail,
                        reply_markup=self._contact_inline_keyboard('service'),
                    )
                    return

                # 2) 目录里没这个口味：直接告诉客户，并列出该型号可选口味
                unresolved = (ai_detail or {}).get('unresolved') or []
                if unresolved:
                    self._retire_noninteractive_failure(context)
                    await update.message.reply_text(
                        self._unresolved_reply_text(unresolved)
                        + '\n\n本次下单失败，请更正后重新发送完整订单信息进行下单。',
                        parse_mode='HTML',
                    )
                    return

                if ai_detail.get('contact_order'):
                    repaired=await self._contact_fields_async(message_text,ai_detail['contact_order'],products=alias_products)
                    recovered=merge_contact(ai_detail['contact_order'],repaired)
                    recovered_status=diagnose_contact(recovered)
                    if not recovered_status['missing'] and not recovered_status['invalid']:
                        valid,reason=OrderParser.validate_order(recovered)
                        if not valid:
                            await update.message.reply_text(reason)
                            return
                        preview=await self._build_order_preview(update,recovered)
                        if preview:
                            from brand_aliases import learn_validated
                            learn_validated(ai_result,preview,products)
                            await self._send_order_preview(update,context,preview)
                        return
                    else:
                        if repaired.get('_name_absent_confirmed'):
                            recovered['_name_absent_confirmed']=True
                        context.user_data['pending_recipient_order'] = {
                            'ai_result': ai_detail['contact_ai'],
                            'order_info': recovered,
                            'raw_text': message_text,
                            'source': self._message_order_source(update),
                        }
                        await self._contact_problem_reply(update,context,recovered,recovered_status)
                        return

                # 其余置信度/商品/人工确认问题不得绕过安全门槛。
                from business_errors import public_order_reasons
                detail = '\n'.join(f'• {reason}' for reason in public_order_reasons(ai_rejection,unresolved=ai_detail.get('unresolved',[]),raw_text=message_text))
                self._retire_noninteractive_failure(context)
                await update.message.reply_text(
                    '❌ 录单失败 订单信息需要补充或修正：\n\n'
                    + detail
                    + '\n\n本次下单不作数，请补充或修正明确的品类、口味、数量及缺失信息后，重新发送完整订单信息进行下单。',
                )
                return

            # DeepSeek 未启用或接口失败时，保留已有本地结构解析作为回退。
            if order_info is None:
                order_info = local_order

            if isinstance(order_info,dict) and learning_ai_state.get('order')=='failed':
                # The order request already exhausted its bounded transport
                # retry. Do not immediately repeat the same provider outage
                # as a separate full-contact request for this message.
                order_info['_order_provider_failed']=True

            if isinstance(order_info, dict) and not self._is_wholesale_bot():
                from learning_evidence import CONTACT_CAPTURE
                capture_token = CONTACT_CAPTURE.set(None)
                with stage('CONTACT'):
                    fields = await self._contact_fields_async(message_text, order_info,products=alias_products)
                contact_evidence = CONTACT_CAPTURE.get() or {}
                CONTACT_CAPTURE.reset(capture_token)
                if contact_evidence.get('called'):
                    learning_ai_state['contact'] = contact_evidence.get('status', 'failed')
                    if contact_evidence.get('snapshot') and not order_info.get('_learning_deepseek_analysis'):
                        order_info['_learning_deepseek_analysis'] = contact_evidence['snapshot']
                order_info['_learning_ai_state'] = learning_ai_state
                order_info['_learning_ai_called'] = any(v != 'not_called' for v in learning_ai_state.values())
                validated_address=str(order_info.get('address') or '').strip()
                order_info = merge_contact(order_info, fields)
                merged_address=str(order_info.get('address') or '').strip()
                if validated_address and merged_address!=validated_address:
                    from address_source_guard import prefer_complete_source_address,validate_explicit_region
                    order_info['address']=prefer_complete_source_address(
                        message_text,merged_address,validated_address,alias_products)
                    validate_explicit_region(message_text,order_info['address'],alias_products)

            if not order_info:
                diagnostic_reject(['UNKNOWN_VALIDATION'])
                if self._is_wholesale_bot():
                    hint = (
                        "❌ 无法识别订单信息，请检查格式是否正确。\n\n"
                        "批发下单示例：\n"
                        "下单\n"
                        "北京市朝阳区xxx街道xxx号\n"
                        "张三\n"
                        "13800138000\n"
                        "悦刻ACE\n"
                        "冰爽铁观音x20\n"
                        "浓郁铁观音20"
                    )
                else:
                    hint = (
                        "❌ 无法识别订单信息，请检查格式是否正确。\n\n"
                        "使用 /help 查看使用说明，或发送：\n"
                        "北京市朝阳区xxx街道xxx号\n"
                        "张三\n"
                        "13800138000\n"
                        "鸭嘴 西瓜x1\n"
                        "鸭嘴 芭乐x2\n\n"
                        "入库示例：\n入库: 苹果 +50, 香蕉 +20"
                    )
                await update.message.reply_text(hint)
                return

            valid, reason = OrderParser.validate_order(order_info)
            if not valid:
                status_for_diagnostics = diagnose_contact(order_info)
                code = 'CONTACT_INVALID' if status_for_diagnostics['missing'] or status_for_diagnostics['invalid'] else (
                    'QUANTITY_INVALID' if order_info.get('quantity_scope_errors') or order_info.get('requires_item_confirmation') else 'UNKNOWN_VALIDATION')
                diagnostic_reject([code])
                from parser_diagnostics import log_rejection
                log_rejection(message_text,order_info,reason='LOCAL_VALIDATION_REJECTED')
                status = diagnose_contact(order_info)
                if reason == contact_prompt(status):
                    context.user_data['pending_recipient_order'] = {
                        'order_info': order_info, 'raw_text': message_text,
                        'source': self._message_order_source(update),
                    }
                    await self._contact_problem_reply(update,context,order_info,status)
                else:
                    await update.message.reply_text(reason)
                return

            preview = await self._build_order_preview(update, order_info)
            if not preview:
                diagnostic_event('FINAL_RESULT', 'REJECTED', ['BUSINESS_REJECTION'])
                return
            diagnostic_event('DETERMINISTIC_VALIDATION', 'SUCCESS')
            if not ai_attempted:
                diagnostic_event('AI_REQUEST', 'NOT_CALLED')
                diagnostic_event('PROVIDER_RESULT', 'NOT_CALLED')
                diagnostic_event('NORMALIZATION_RESULT', 'SKIPPED')
            diagnostic_event('FINAL_RESULT', 'SUCCESS' if ai_result else ('LOCAL_FALLBACK' if ai_attempted else 'LOCAL_ONLY'), local_only=not ai_attempted)
            if ai_result and not ai_rejection:
                try:
                    from brand_aliases import learn_validated
                    learn_validated(ai_result, preview, products)
                except Exception as learning_error:
                    # Optional learning must never abort a validated order.
                    logger.warning('品牌叫法学习候选未保存 type=%s', type(learning_error).__name__)
            await self._send_order_preview(update, context, preview)

        except TelegramError:
            # Application error handler applies notification suppression; do
            # not amplify a limited/failed send with an immediate error send.
            raise
        except ExecutionStopped:
            # The durable queue owns the final receipt. A second customer
            # reply here is fenced too and would hide the original stop stack.
            from execution_safety import CURRENT as execution_current
            state = execution_current.get() or {}
            category = state.get('technical_error')
            allowed = {'TIMEOUT', 'NETWORK_ERROR', 'AUTH_ERROR', 'CREDIT_ERROR',
                       'RATE_LIMIT', 'URL_ERROR', 'MODEL_ERROR', 'PROVIDER_ERROR',
                       'INVALID_RESPONSE', 'KEY_MISSING', 'AI_DISABLED',
                       'HTTP_SERVER_ERROR', 'HTTP_CONNECTION_ERROR', 'HTTP_TIMEOUT',
                       'ADDRESS_BUDGET_EXHAUSTED', 'ADDRESS_MONTHLY_BUDGET_EXHAUSTED',
                       'ADDRESS_SERVICE_FAILED', 'ADDRESS_DEADLINE_EXCEEDED',
                       'ADDRESS_CASH_BUDGET_EXHAUSTED', 'ADDRESS_SERVICE_CIRCUIT_OPEN', 'ADDRESS_COST_GUARD_UNAVAILABLE', 'ADDRESS_USAGE_UNAVAILABLE'}
            allowed.update(('ADDRESS_ACCOUNT_BALANCE_INSUFFICIENT','ADDRESS_PROVIDER_RATE_LIMITED'))
            safe_category = category if isinstance(category, str) and category in allowed else 'EXECUTION_STOPPED'
            from ai_diagnostics import CODES
            if category:
                diagnostic_event('FINAL_RESULT', 'FAILED',
                                 [safe_category] if safe_category in CODES else [])
                logger.warning('ORDER_TECHNICAL_CLOSED category=%s', safe_category)
            else:
                diagnostic_event('FINAL_RESULT', 'REJECTED', ['UNKNOWN_VALIDATION'])
                logger.info('ORDER_EXECUTION_CLOSED category=%s', safe_category)
            raise
        except Exception as e:
            context._rainbow_processing_failed = True
            diagnostic_event('FINAL_RESULT', 'FAILED', ['INTERNAL_ERROR'])
            logger.error('处理订单消息失败 kind=%s',type(e).__name__)
            await update.message.reply_text('❌ 处理失败，请稍后重试或联系管理员。')
        finally:
            if not self._is_wholesale_bot():
                record_contact_evidence('ORDER_FINAL',locals().get('order_info'),message_text)
                try:
                    from ai_diagnostics import CURRENT as diagnostic_current
                    from failure_analysis import record as record_recognition_review
                    terminal = diagnostic_current.get() or {}
                    outcome = terminal.get('_terminal_outcome')
                    errors = list(terminal.get('_terminal_codes') or [])
                    snapshot = locals().get('local_evidence')
                    if isinstance(snapshot, dict):
                        valid, reason = OrderParser.validate_order(dict(snapshot))
                        if not valid:
                            errors.append(str(reason))
                    errors.extend(str(e) for e in (locals().get('ai_rejection') or []))
                    errors.extend(REJECTION_REPLIES.get() or [])
                    review_identity=await asyncio.to_thread(record_recognition_review,
                        source=[getattr(update.effective_chat,'id',None),
                                getattr(update.effective_user,'id',None), update.message.message_id],
                        original=message_text, local_result=snapshot,
                        ai_result=(locals().get('provider_evidence') or locals().get('ai_result') or
                                   (locals().get('contact_evidence') or {}).get('snapshot')),
                        catalog=locals().get('alias_products') or (),
                        errors=errors or ([] if outcome in ('SUCCESS','LOCAL_ONLY','LOCAL_FALLBACK')
                                          else ['RECOGNITION_INCOMPLETE']),
                        outcome=('RECOGNIZED' if outcome in ('SUCCESS','LOCAL_ONLY','LOCAL_FALLBACK')
                                 else 'NEEDS_CONFIRMATION' if outcome=='AWAITING_VALIDATION'
                                 else 'FAILED' if outcome == 'FAILED' else 'REJECTED'))
                    if review_identity and locals().get('typo') and outcome=='AWAITING_VALIDATION':
                        from failure_analysis import save_confirmation_suggestions
                        save_confirmation_suggestions(review_identity,typo[1],locals().get('alias_products') or ())
                except Exception as error:
                    logger.warning('RECOGNITION_REVIEW_CAPTURE_FAILED type=%s',type(error).__name__)
            REJECTION_REPLIES.reset(rejection_reply_token)
            diagnostic_scope.__exit__(None, None, None)

    @staticmethod
    def _ai_runtime_config():
        try:
            return get_ai_settings({'enabled': DEEPSEEK_ENABLED, 'order_enabled': DEEPSEEK_ENABLED, 'contact_enabled': DEEPSEEK_ENABLED})
        except AIConfigError:
            logger.warning('AI configuration unavailable; deterministic parser remains enabled')
            return {'enabled': False, 'order_enabled': False, 'contact_enabled': False}

    @__import__('order_trace').timed_async('CONTACT_RECOVERY')
    async def _contact_fields_async(self, text, draft=None, waiting=None, products=()):
        # A previous technical failure in this task is authoritative. Do not
        # start a second paid contact/address pass before the terminal fence.
        from execution_safety import ensure_customer_validation
        ensure_customer_validation()
        with diagnostic_request('CONTACT_PARSE', text):
            fields=await self._contact_fields_diagnostic_scope(text, draft, waiting, products=products)
            if self._is_wholesale_bot():return fields
            # Valid local addresses never wait for the cloud. Only an invalid
            # address can use the separately configured address-only fallback.
            contact=merge_contact(draft,fields)
            original=str(contact.get('raw_address') or contact.get('address') or '')
            status=diagnose_contact(contact) if original else {'missing': [], 'invalid': []}
            if original and 'address' in status['missing']+status['invalid']:
                try:
                    from verified_address_cache import suggestions
                    fields['_address_cache_candidates']=suggestions((draft or {}).get('telegram_id'),original)
                except Exception:
                    fields['_address_cache_candidates']=[]
                from address_contact import enrich
                fields=await enrich(fields,draft)
            record_contact_evidence('CONTACT_FINAL',merge_contact(draft,fields),text)
            return fields

    async def _resume_address_supplement(self,update,context,text):
        """Only explicit, owned contact replies advance their original draft."""
        original=getattr(update.message,'reply_to_message',None)
        address_reply=bool(re.search(r'省|市|区|县|街道|镇|乡|路|街|小区|花园|公寓|苑|号|栋|幢|楼|室|单元',text))
        name_reply=bool(re.fullmatch(r'\s*(?:(?:姓名|收件人)\s*[:：]\s*[\u4e00-\u9fffA-Za-z· ]{2,20}|[\u4e00-\u9fff]{1,6}(?:先生|女士))\s*',text))
        if not original or not (address_reply or name_reply):return False
        account=await self._api_call('get_telegram_account',telegram_id=str(update.effective_user.id))
        if not isinstance(account,dict) or not account.get('authorized'):return False
        ok,result=await self._api_call('open_group_draft',update.effective_chat.id,
            update.message.message_id,update.effective_user.id,new_order=False,
            reply_message_id=original.message_id,
            **({'resume':True} if getattr(context,'_rainbow_queue_resume',False) else {}))
        draft=result.get('draft') if ok and isinstance(result,dict) else None
        if not draft:return False
        from telegram_order_context import AccountUpdate,DraftContext
        scoped=DraftContext(context,draft)
        pending=scoped.user_data.get('pending_recipient_order') or {}
        if pending.get('waiting_field')=='address':
            if not address_reply:return False
        else:
            issues=diagnose_contact(pending.get('order_info') or {})
            if not name_reply or issues['missing']+issues['invalid']!=['recipient']:return False
        try:
            await self._handle_pending_recipient_reply(AccountUpdate(update,account,draft),scoped,text)
        finally:
            completed=not any(scoped.user_data.get(k) for k in ('pending_recipient_order','pending_order','pending_manual_review','pending_ai_flavor'))
            saved,_=await self._api_call('save_group_draft',update.effective_chat.id,draft['token'],
                scoped.user_data,completed,sender_id=str(update.effective_user.id))
            if not saved:raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')
        return True

    async def _contact_problem_reply(self,update,context,order,status):
        callback=getattr(update,'callback_query',None)
        async def reply(*args,**kwargs):
            if callback:sent=await callback.edit_message_text(*args,**kwargs)
            else:sent=await update.message.reply_text(*args,**kwargs)
            from draft_button_evidence import record
            record(context.user_data,kwargs.get('reply_markup'),getattr(sent,'message_id',None))
            return sent
        from contact_fields import needs_address_region,needs_address_detail
        if 'address' in status['missing']+status['invalid']:
            proposal=order.get('_cloud_address_proposal')
            token=context.user_data.get('_group_draft_token')
            if isinstance(proposal,dict) and token:
                pending=context.user_data.setdefault('pending_recipient_order',dict(order_info=order))
                pending['waiting_field']='cloud_address'
                await reply('⚠️ 请确认阿里云识别的收货地址：\n'+str(proposal.get('address') or '')+
                    '\n\n原地址：'+str(proposal.get('source_address') or '')+
                    '\n确认前未创建订单、未扣库存；如地址不正确请取消并重发完整订单。',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('确认地址并继续下单',callback_data='address_confirm:'+token),
                        InlineKeyboardButton('取消订单',callback_data='draft_cancel:'+token)]]))
                return
            self._retire_noninteractive_failure(context)
            from offline_address import problem_text
            text='❌ 录单失败 收件信息需要补充或修正：\n• '+problem_text(status)
            candidates=[]
            try:
                from verified_address_cache import suggestions
                principal=getattr(getattr(update,'effective_user',None),'id',None)
                candidates=await asyncio.to_thread(suggestions,principal,order.get('raw_address') or order.get('address'))
            except Exception:pass
            if candidates:
                text+='\n\n历史已核实地址候选（未套用，请核对后重发完整订单）：\n'+'\n'.join('• '+str(c) for c in candidates[:3])
            await reply(text)
            return
        from contact_fields import name_generation_allowed, suspected_name
        if name_generation_allowed(order,status):
            token=context.user_data.get('_group_draft_token')
            if token:
                buttons=[[InlineKeyboardButton('确认生成',callback_data='name_generate:'+token),
                          InlineKeyboardButton('取消订单',callback_data='draft_cancel:'+token)]]
                candidate=suspected_name(order)
                prompt=('录单失败\n当前检测到疑似姓名：'+candidate+'，请问是否使用？' if candidate else
                        '⚠️ 当前收件人姓名不符合规则或缺失，是否确认替换为管理员配置的临时收件名称？\n也可补充真实姓名后重新发送订单。')
                await reply(prompt,
                    reply_markup=InlineKeyboardMarkup(buttons))
                return
        self._retire_noninteractive_failure(context)
        await reply('❌ 录单失败\n'+contact_prompt(status).removeprefix('❌ ')
                    +'\n\n本次下单不作数，请修正后重新发送完整订单信息进行下单。')

    async def handle_name_generate(self,update,context):
        query=update.callback_query
        await query.answer()
        if not await self._current_order_eligibility(update):
            await query.answer('下单资格已失效，未执行姓名确认。');return
        parts=str(query.data or '').split(':',1)
        if len(parts)!=2:
            await query.answer('按钮已失效，请使用当前草稿的按钮。');return
        draft=await self._api_call('get_group_draft',update.effective_chat.id,parts[1],
                                   sender_id=str(update.effective_user.id),audit_event_id=getattr(update,'update_id',None),
                                   audit_action='address_confirm' if str(query.data or '').startswith('address_confirm:') else 'name_generate',audit_message_id=getattr(getattr(query,'message',None),'message_id',None))
        if not isinstance(draft,dict):
            await query.answer('草稿已处理、过期或无操作权限。');return
        from telegram_order_context import DraftContext,AccountUpdate
        account=await self._api_call('get_telegram_account',telegram_id=str(update.effective_user.id))
        if not isinstance(account,dict) or not account.get('authorized'):
            await query.answer('下单资格已失效，未执行姓名确认。');return
        scoped=DraftContext(context,draft)
        pending=scoped.user_data.get('pending_recipient_order') or {}
        order=pending.get('order_info') or {}
        status=diagnose_contact(order)
        address_confirmation=str(query.data or '').startswith('address_confirm:')
        if address_confirmation:
            proposal=order.get('_cloud_address_proposal')
            from cloud_address_trust import safety,remember
            if not isinstance(proposal,dict) or not safety(proposal.get('source_address'),proposal.get('address'),proposal.get('cloud_fields') or {}):
                await query.answer('地址确认已处理或候选已失效，请重新发送完整订单。');return
            remember(proposal['address'])
            order.update(address=proposal['address'],raw_address=proposal['source_address'])
            order.pop('_cloud_address_proposal',None)
        from contact_fields import name_generation_allowed, suspected_name
        if not address_confirmation and not name_generation_allowed(order,status):
            await query.answer('姓名确认已处理或当前草稿仍需补充其他信息。')
            return
        # The generated fallback is deliberately stable across callback
        # retries.  A random value here can make the same durable draft create
        # different order facts after a timeout/retry boundary.
        from recipient_policy import temporary_name
        candidate=suspected_name(order)
        name='' if address_confirmation else candidate or temporary_name(order)
        if not address_confirmation and not name:
            await query.answer('当前未配置可用的临时名称，或物流要求真实姓名，请补充收件人姓名。')
            return
        if not address_confirmation and not candidate:
            order.update(recipient=None,_recipient_generated=True,
                         _recipient_source='ADMIN_TEMPORARY',_recipient_learning_excluded=True)
        # The customer's explicit button authorizes only generation of a name;
        # all existing contact/catalog/address/stock/routing validators still run.
        class ReplyMessage:
            def __getattr__(self,key):return getattr(query.message,key)
            async def reply_text(self,*args,**kwargs):
                # Message.reply_text accepts reply targets; editing the
                # existing callback receipt does not. Keep the result on its
                # exact prompt without passing send-only parameters to PTB.
                for key in ('reply_to_message_id','reply_parameters','allow_sending_without_reply','message_thread_id'):
                    kwargs.pop(key,None)
                kwargs.setdefault('reply_markup',None)
                return await query.edit_message_text(*args,**kwargs)
        class CallbackUpdate:
            message=ReplyMessage()
            effective_message=message
            def __getattr__(self,key):return getattr(update,key)
        try:
            await self._handle_pending_recipient_reply(AccountUpdate(CallbackUpdate(),account,draft),scoped,name)
        finally:
            completed=not any(scoped.user_data.get(k) for k in ('pending_recipient_order','pending_order','pending_ai_flavor','pending_manual_review'))
            saved,_=await self._api_call('save_group_draft',update.effective_chat.id,draft['token'],
                                scoped.user_data,completed,sender_id=str(update.effective_user.id))
            if not saved:raise RuntimeError('ORDER_DRAFT_SAVE_FAILED')

    async def _contact_fields_diagnostic_scope(self, text, draft=None, waiting=None, products=()):
        supplied = parse_contact(text, waiting, products=products)
        from contact_fields import contact_catalog_names
        merchandise_names = contact_catalog_names(products)
        # A complete phone immediately after an institution/name span proves
        # a reasonable candidate even during provider failure. A unique valid
        # source span can be used directly; ambiguous candidates stay pending.
        if not supplied.get('recipient') and not self._is_wholesale_bot():
            from contact_fields import valid_name
            candidates=set()
            for line in str(text).splitlines():
                if not re.search(r'省|市|区|县',line):continue
                for match in re.finditer(r'(?:宠物店|商店|门店|便利店|超市|公司|学校|医院|驿站|酒店|宾馆)'
                        r'([\u4e00-\u9fffA-Za-z]{2,6})(?=1[3-9]\d{9}(?!\d))',line):
                    value=match.group(1)
                    if valid_name(value) and value.casefold() not in merchandise_names and name_has_evidence(value,text):
                        candidates.add(value)
            if len(candidates)==1:
                supplied['_suspected_recipient']=candidates.pop()
                supplied['_suspected_recipient_source']=text
                supplied=merge_contact(supplied,dict(recipient=supplied['_suspected_recipient'],
                                                    _recipient_source='SOURCE_LOCAL'))
        if (draft and str(draft.get('recipient') or '').casefold() in merchandise_names
                and not supplied.get('recipient')):
            draft = dict(draft, recipient=None)
            supplied['recipient'] = None
        local = merge_contact(draft, supplied)
        if not local.get('recipient'):
            from contact_fields import with_generated_name
            supplied=with_generated_name(local)
            local=merge_contact(draft,supplied)
        status = diagnose_contact(local)
        if not self._is_wholesale_bot():
            record_contact_evidence('CONTACT_LOCAL',local,text)
        from contact_fields import deterministic_name_absence
        if (status['missing']==['recipient'] and not status['invalid']
                and not any(local.get(key) for key in ('quantity_scope_errors','requires_item_confirmation','multiple_orders_detected'))
                and deterministic_name_absence(text,local,products)):
            # Missing-name generation is local only after exhaustive source
            # proof. Merchandise and transactional guards still run afterward.
            from contact_fields import with_generated_name
            supplied=with_generated_name(merge_contact(draft,supplied))
            supplied['_name_absent_confirmed']=True
            diagnostic_event('AI_REQUEST','NOT_CALLED')
            diagnostic_event('PROVIDER_RESULT','NOT_CALLED')
            return supplied
        settings = self._ai_runtime_config()
        diagnostic_event('LOCAL_RESULT', 'REJECTED' if status['missing'] or status['invalid'] else 'SUCCESS')
        if not settings['enabled'] or not settings['contact_enabled'] or self._is_wholesale_bot() or (draft or {}).get('_order_provider_failed') or not (status['missing'] or status['invalid']):
            diagnostic_event('AI_REQUEST', 'NOT_CALLED')
            diagnostic_event('PROVIDER_RESULT', 'NOT_CALLED')
            diagnostic_event('NORMALIZATION_RESULT', 'SKIPPED')
            diagnostic_event('DETERMINISTIC_VALIDATION', 'REJECTED' if status['missing'] or status['invalid'] else 'SUCCESS',
                             ['CONTACT_INVALID'] if status['missing'] or status['invalid'] else [])
            diagnostic_event('FINAL_RESULT', 'REJECTED' if status['missing'] or status['invalid'] else 'LOCAL_ONLY',
                             ['CONTACT_INVALID'] if status['missing'] or status['invalid'] else [], local_only=True)
            return supplied
        diagnostic_event('AI_REQUEST', 'PENDING', contact_fallback=True)
        from learning_evidence import CONTACT_CAPTURE
        CONTACT_CAPTURE.set({'called': True, 'status': 'failed'})
        try:
            from order_trace import stage
            from ai_transport import run_provider_worker
            with stage('CONTACT_AI'):
                candidate = await run_provider_worker(
                    parse_contact_ai, text, products) if products else await run_provider_worker(parse_contact_ai, text)
            if isinstance(candidate, dict):
                record_contact_evidence('CONTACT_PROVIDER',candidate,text)
                CONTACT_CAPTURE.set({'called': True, 'status': 'returned', 'snapshot': __import__('copy').deepcopy(candidate)})
            if not isinstance(candidate, dict) or float(candidate.get('confidence') or 0) < DEEPSEEK_MIN_CONFIDENCE:
                if isinstance(candidate, dict):
                    captured = CONTACT_CAPTURE.get() or {}
                    CONTACT_CAPTURE.set(dict(captured, status='rejected'))
                diagnostic_reject(['CONFIDENCE_REJECTED' if isinstance(candidate,dict) else 'INVALID_RESPONSE'])
                return supplied
            candidate_status = diagnose_contact(candidate)
            if ('recipient' in status['missing'] and candidate.get('name_absent') is True
                    and not candidate.get('recipient') and not parse_contact(text,products=products).get('recipient')):
                supplied['_name_absent_confirmed']=True
                from contact_fields import with_generated_name
                supplied=with_generated_name(supplied)
            evidence_failed = False
            for field in candidate_status['recognized']:
                # Deterministic success wins. AI may only repair unresolved
                # fields and must have evidence in this message.
                if field in status['recognized']:
                    continue
                value = str(candidate[field]).strip()
                if (field == 'recipient' and value.casefold() in merchandise_names
                        and str(supplied.get('recipient') or '').casefold() != value.casefold()):
                    evidence_failed = True
                    continue
                if self._contact_candidate_evidence(field, value, text):
                    supplied[field] = value
                    if field=='recipient':
                        supplied['_recipient_source']='SOURCE_AI'
                else:
                    evidence_failed = True
            # A phone-adjacent recipient recovered by AI may still be at the
            # end of the locally extracted address. Consume only that proven
            # recipient span, leaving the delivery detail untouched.
            recovered_name = str(supplied.get('recipient') or supplied.get('_suspected_recipient') or '').strip()
            if recovered_name and name_has_evidence(recovered_name, text):
                phone = str(local.get('phone') or supplied.get('phone') or '')
                if phone and re.search(re.escape(recovered_name) + r'\s*[:：]?\s*' + re.escape(phone), text):
                    for address_field in ('address', 'raw_address'):
                        address_value = str(supplied.get(address_field) or local.get(address_field) or '')
                        if address_value.endswith(recovered_name):
                            supplied[address_field] = address_value[:-len(recovered_name)].rstrip()
            final_status = diagnose_contact(merge_contact(draft, supplied))
            codes = (['CONTACT_INVALID'] if candidate_status['invalid'] or final_status['missing'] or final_status['invalid'] else [])
            if evidence_failed:
                codes.append('SOURCE_EVIDENCE_FAILED')
            if codes:
                CONTACT_CAPTURE.set(dict(CONTACT_CAPTURE.get() or {}, status='rejected'))
                diagnostic_reject(codes)
            else:
                CONTACT_CAPTURE.set(dict(CONTACT_CAPTURE.get() or {}, status='validated'))
                diagnostic_event('DETERMINISTIC_VALIDATION', 'SUCCESS')
                diagnostic_event('FINAL_RESULT', 'SUCCESS')
        except Exception as error:
            # Never stringify provider errors: they can include credentials
            # or customer data. Preserve all deterministic fields.
            logger.warning('AI contact fallback type=%s', type(error).__name__)
            from contact_fields import deterministic_name_absence
            if ('recipient' in status['missing']
                    and deterministic_name_absence(text, supplied, products)):
                supplied['_name_absent_confirmed'] = True
                from contact_fields import with_generated_name
                supplied=with_generated_name(supplied)
        return supplied

    @staticmethod
    def _contact_candidate_evidence(field, value, text):
        compact = re.sub(r'\s+', '', str(text))
        value = str(value).strip()
        if field == 'recipient':
            return name_has_evidence(value, text)
        if field != 'address':
            return bool(value) and re.sub(r'\s+', '', value) in compact
        from address_utils import normalize_cn_address, CITY_TO_PROVINCE
        meta = normalize_cn_address(value)
        regions = [str(meta.get('province') or '').rstrip('省市'), str(meta.get('city') or '').rstrip('市')]
        known_province=CITY_TO_PROVINCE.get(meta.get('city'))
        if known_province and known_province!=meta.get('province'):
            return False
        return bool(meta.get('district') and meta.get('detail')) and meta['district'] in compact and meta['detail'] in compact and any(region and region != '市辖区' and region in compact for region in regions)

    def _parse_quick_delivery_info(self, text: str) -> Optional[Dict]:
        from contact_fields import with_generated_name
        fields = with_generated_name(parse_contact(text))
        status = diagnose_contact(fields)
        if not status['missing'] and not status['invalid']:
            return fields
        return None

    async def _handle_wholesale_cart_delivery(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_text: str,
    ) -> bool:
        checkout_items = context.user_data.get('wholesale_cart_checkout') or []
        if not isinstance(checkout_items, list) or not checkout_items:
            context.user_data.pop('wholesale_cart_checkout', None)
            return False

        delivery = self._parse_quick_delivery_info(message_text)
        if not delivery:
            await update.message.reply_text(
                "❌ 未识别到完整收货信息，请按三行发送：\n"
                "地址\n姓名\n手机号"
            )
            return True

        items = []
        for row in checkout_items:
            try:
                product_id = int(row.get('product_id'))
                quantity = int(row.get('quantity') or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if product_id > 0 and quantity > 0:
                items.append(({'product_id': product_id}, quantity))
        if not items:
            context.user_data.pop('wholesale_cart_checkout', None)
            await update.message.reply_text("❌ 购物车为空，请重新选择商品。")
            return True

        order_info = {'items': items, **delivery}
        valid, reason = OrderParser.validate_order(order_info)
        if not valid:
            await update.message.reply_text(reason)
            return True
        preview = await self._build_order_preview(update, order_info)
        context.user_data.pop('wholesale_cart_checkout', None)
        if not preview:
            return True
        await self._send_order_preview(update, context, preview, source='wholesale_cart')
        return True

    @staticmethod
    def _message_order_source(update):
        chat=getattr(update,'effective_chat',None)
        message=getattr(update,'effective_message',None) or getattr(update,'message',None)
        return dict(source_chat_id=str(chat.id) if chat else None,
                    source_message_id=getattr(update,'_rainbow_source_message_id',getattr(message,'message_id',None)),
                    source_chat_type=getattr(chat,'type',None),
                    source_chat_title=getattr(chat,'title',None))

    async def _handle_quick_order_delivery(self, update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str) -> bool:
        quick = context.user_data.get('quick_order_item') or {}
        if message_text in ('取消', '算了', '不发了', '不要了'):
            context.user_data.pop('quick_order_item', None)
            await update.message.reply_text('好的，这单先不下了。')
            return True
        previous = quick.get('contact') or {}
        status = diagnose_contact(previous)
        waiting = (status['missing'] + status['invalid'])[0] if status['missing'] or status['invalid'] else None
        delivery = merge_contact(previous, await self._contact_fields_async(message_text, previous, waiting))
        quick['contact'] = delivery
        status = diagnose_contact(delivery)
        if status['missing'] or status['invalid']:
            await update.message.reply_text(contact_prompt(status),reply_markup=self._draft_cancel_keyboard(context))
            return True
        order_info = {
            'items': [({
                'product_id': quick.get('product_id'),
                'product_name': quick.get('product_name'),
                'parent_name': quick.get('parent_name'),
            }, int(quick.get('quantity') or 1))],
            **delivery,
        }
        valid, reason = OrderParser.validate_order(order_info)
        if not valid:
            await update.message.reply_text(reason)
            return True
        preview = await self._build_order_preview(update, order_info)
        if not preview:
            if order_info.get('_product_limit_failed'):
                self._retire_noninteractive_failure(context)
            return True
        context.user_data.pop('quick_order_item', None)
        await self._send_order_preview(update, context, preview)
        return True

    @staticmethod
    def _only_recipient_is_missing(
        ai_result: Dict,
        errors: List[str],
        detail: Dict,
    ) -> bool:
        """判断这一单是否「只差收件人姓名」，其余全部通过。

        只有这种情况才值得追问客户——否则补个名字回来照样下不了单，白问一轮。
        判据是把「缺收件人」和「需人工确认」这两条拿掉后，错误列表必须为空。
        """
        if not isinstance(ai_result, dict):
            return False
        if (detail or {}).get('is_aftersale') or (detail or {}).get('unresolved'):
            return False
        if [str(x).strip() for x in (ai_result.get('missing') or [])] != ['recipient']:
            return False
        leftover = [
            error for error in (errors or [])
            if 'recipient' not in error
            and '需要人工确认' not in error
            and '未能确定字段' not in error
        ]
        return not leftover

    async def _handle_pending_recipient_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_text: str,
    ) -> bool:
        """收件人缺失的自动追问：客户回复姓名后补全并重跑完整校验。"""
        pending = context.user_data.get('pending_recipient_order') or {}

        if message_text in ('取消', '算了', '不发了', '不要了'):
            context.user_data.pop('pending_recipient_order', None)
            await update.message.reply_text('好的，这单先不下了，需要时再发给我 🙏')
            return True

        ai_result = dict(pending.get('ai_result') or {})
        raw_text = str(pending.get('raw_text') or '')
        draft = pending.get('order_info') or ai_result
        if not draft:
            await update.message.reply_text('❌ 这单信息已过期，麻烦重新发一次 🙏')
            return True
        status = diagnose_contact(draft)
        issues = status['missing'] + status['invalid']
        if pending.get('waiting_field')=='address':
            # Historical address prompts remain audit records until reviewed.
            # A detail-only reply must not resume an old source under the new
            # complete-resend policy or imply that a committed order failed.
            await update.message.reply_text(
                '旧版地址补充流程已停止。请补全收件信息后重新发送完整订单；'
                '如原消息已生成订单，请联系管理员核对，避免重复下单。')
            return True
        else:
            supplied = await self._contact_fields_async(message_text, draft, issues[0] if issues else None)
        order_info = merge_contact(draft, supplied)
        callback=getattr(update,'callback_query',None)
        if callback and str(getattr(callback,'data','') or '').startswith('name_generate:'):
            from recipient_policy import temporary_name
            if draft.get('_recipient_generated') and message_text==temporary_name(draft):
                order_info.update(_recipient_generated=True,_recipient_source='ADMIN_TEMPORARY',
                                  _recipient_learning_excluded=True)
        pending['order_info'] = order_info
        status = diagnose_contact(order_info)
        if status['missing'] or status['invalid']:
            await self._contact_problem_reply(update,context,order_info,status)
            return True
        if ai_result:
            contact_snapshot={f:order_info.get(f) for f in (*FIELDS,'_recipient_generated',
                '_recipient_source','_recipient_learning_excluded','_requires_real_name') if f in order_info}
            ai_result = merge_contact(ai_result, contact_snapshot)
            ai_result['missing'] = [x for x in ai_result.get('missing') or [] if x not in FIELDS]
            # This draft was created only after all non-contact gates passed.
            ai_result['needs_manual_review'] = False
            pending['ai_result'] = ai_result
            products, _visible_warehouses, _visible_user = await self._visible_catalog_for_user_async(update)
            if not products:
                await update.message.reply_text('❌ 后台商品目录读取失败，请稍后重试；草稿已保留。')
                return True
            # Keep original product evidence, but use the merged contact
            # snapshot instead of parsing old contact values over corrections.
            order_info, errors, _detail = await recognition_work(self._validate_ai_order_against_catalog,
                ai_result, raw_text, products,
                contact_override=contact_snapshot,
                customer_confirmed=tuple(pending.get('customer_confirmed') or ()),
            )
            if not order_info:
                await update.message.reply_text('⚠️ 订单仍需确认：\n' + '\n'.join(errors[:6]))
                return True
        elif not order_info.get('bundle_requests'):
            valid, reason = OrderParser.validate_order(order_info)
            if not valid:
                await update.message.reply_text(reason)
                return True

        preview = await self._build_order_preview(update, order_info)
        if not preview:
            if order_info.get('_product_limit_failed'):
                self._retire_noninteractive_failure(context)
            return True
        if pending.get('source'):
            preview.update(pending['source'])
        callback=getattr(update,'callback_query',None)
        if callback and str(getattr(callback,'data','') or '').startswith(('name_generate:','address_confirm:')):
            preview['_receipt_message']=callback.message
        if ai_result:
            try:
                from brand_aliases import learn_validated
                learn_validated(ai_result, preview, products)
            except Exception as learning_error:
                logger.warning('品牌叫法学习候选未保存 type=%s', type(learning_error).__name__)
        await self._send_order_preview(update, context, preview)
        context.user_data.pop('pending_recipient_order', None)
        return True

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Only order-caption text is eligible; never download/OCR media."""
        caption = str(getattr(update.message, 'caption', None) or '').strip()
        if not caption or not self._looks_like_order_candidate(caption):
            logger.info('MEDIA_IGNORED')
            return
        # Preserve sender/chat/message/reply identity and all existing permission
        # and draft gates. A proxy substitutes only text, not media content.
        class CaptionMessage:
            text = caption
            def __getattr__(self, name):
                return getattr(update.message, name)
        class CaptionUpdate:
            message = CaptionMessage()
            effective_message = message
            def __getattr__(self, name):
                return getattr(update, name)
        if getattr(self,'_queue_runtime',None):
            await self.receive_order_message(update,context)
        else:
            await self.handle_message(CaptionUpdate(), context)
        return

    async def _legacy_inventory_photo_disabled(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unregistered historical adapter, retained for recoverable audit."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        # 检查管理员权限
        if not self._is_admin(user_id):
            await update.message.reply_text("❌ 您没有权限使用此功能")
            return

        # 发送处理中消息
        processing_msg = await update.message.reply_text("📸 正在处理图片...")

        # 获取图片
        photo = update.message.photo[-1]  # 获取最大尺寸的图片
        file = await photo.get_file()

        # 下载图片到临时文件
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        temp_path = temp_file.name
        temp_file.close()

        try:
            await file.download_to_drive(temp_path)

            # 调用API处理图片
            api_url = f"{API_BASE_URL}/api/inventory/process_image"

            with open(temp_path, 'rb') as f:
                files = {'file': ('inventory.jpg', f, 'image/jpeg')}
                data = {
                    'warehouse_name': '默认仓库',  # 可以后续扩展让用户选择
                    'incremental': True
                }

                response = await asyncio.to_thread(
                    requests.post, api_url, files=files, data=data, timeout=30,
                )

            if response.status_code == 200:
                result = response.json()

                if result.get('success'):
                    update_result = result.get('result', {})
                    success_count = update_result.get('success_count', 0)
                    failed_count = update_result.get('failed_count', 0)
                    warehouse_name = update_result.get('warehouse_name', '未知')

                    # 构建结果消息
                    message = f"✅ <b>库存更新完成！</b>\n\n"
                    message += f"📦 仓库: {warehouse_name}\n"
                    message += f"✅ 成功: {success_count} 条\n"
                    message += f"❌ 失败: {failed_count} 条\n"

                    if success_count > 0:
                        message += f"\n<b>更新详情：</b>\n"
                        items = update_result.get('items', [])
                        for item in items[:10]:  # 只显示前10条
                            if item.get('action') in ['updated', 'created']:
                                action = '➕' if item['action'] == 'created' else '🔄'
                                message += f"{action} {item['product_name']}: {item.get('message', '')}\n"

                        if len(items) > 10:
                            message += f"...还有 {len(items) - 10} 条\n"

                    await processing_msg.edit_text(message, parse_mode='HTML')
                else:
                    await processing_msg.edit_text(f"❌ 处理失败: {result.get('error', '未知错误')}")
            else:
                await processing_msg.edit_text(f"❌ API错误: {response.status_code}")

        except requests.exceptions.Timeout:
            await processing_msg.edit_text("❌ 处理超时，请稍后重试")
        except Exception as e:
            logger.error(f"处理库存图片失败: {e}")
            await processing_msg.edit_text(f"❌ 处理失败: {str(e)}")
        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """错误处理"""
        error_text = clean_telegram_text(context.error)
        lowered = error_text.lower()
        if 'message is not modified' in lowered or 'query is too old' in lowered:
            logger.info('忽略 Telegram 状态竞态: %s', error_text)
            return
        error = context.error
        import traceback
        frames=[(os.path.basename(f.filename),f.name,f.lineno)
                for f in traceback.extract_tb(error.__traceback__)]
        logger.error('发生错误 kind=%s stack=%s',type(error).__name__,frames)

        limiter = getattr(getattr(context, 'bot', None), 'rate_limiter', None)
        if (update and update.effective_message
                and (not limiter or limiter.allow_error_notice(
                    update.effective_chat.id if update.effective_chat else None, error))):
            try:
                await update.effective_message.reply_text("❌ 发生错误，请稍后重试。")
            except Exception as exc:
                logger.warning("发送错误提示失败: %s", clean_telegram_text(exc))

    def run(self):
        """运行机器人"""
        from newbot_safety import startup_guard
        self._newbot_instance_guard=startup_guard()
        if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN in {"YOUR_BOT_TOKEN_HERE", "TOKEN", "BOT_TOKEN"}:
            print("❌ 请先设置 BOT_TOKEN（或兼容旧名称 TELEGRAM_BOT_TOKEN）")
            print("1. 在 Telegram 中搜索 @BotFather")
            print("2. 使用 /newbot 命令创建机器人")
            print("3. 将 Token 写入 .env")
            return

        from order_trace import ScopedUpdateProcessor
        from telegram_queue_runtime import IngressQueue
        from config import telegram_send_request
        self.app = (
            ApplicationBuilder()
            .token(TELEGRAM_BOT_TOKEN)
            .request(telegram_send_request())
            .update_queue(IngressQueue())
            .concurrent_updates(ScopedUpdateProcessor(32))
            .rate_limiter(TelegramDeliveryLimiter(state=DeliveryState()))
            .post_init(self._start_durable_queue)
            .post_stop(self._stop_durable_queue)
            .build()
        )

        # 注册命令处理器
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("menu", self.menu))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("balance", self.balance))
        self.app.add_handler(CommandHandler("debt", self.debt))
        self.app.add_handler(CommandHandler("order", self.order_guide))
        self.app.add_handler(CommandHandler("stats", self.stats_command))
        self.app.add_handler(CommandHandler("products", self.products))
        self.app.add_handler(CommandHandler("stock", self.stock))
        self.app.add_handler(CommandHandler("orders", self.orders))
        self.app.add_handler(CommandHandler("cancel", self.cancel))
        self.app.add_handler(CommandHandler("chatid", self.chatid))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback_ingress))

        # 注册消息处理器
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.receive_order_message
        ))

        # 注册图片处理器（库存更新）
        self.app.add_handler(MessageHandler(
            filters.PHOTO,
            self.handle_photo
        ))

        # 注册错误处理器
        self.app.add_error_handler(self.error_handler)

        # 检查Webhook服务器
        if self.use_webhook:
            from web_uploader import WebUploader
            if WebUploader.check_webhook_available():
                logger.info("✅ Webhook服务器可用")
            else:
                logger.warning("⚠️ Webhook服务器不可用，将跳过订单上传")

        # 启动机器人
        logger.info("🚀 机器人启动成功！")

        if self.use_webhook:
            # 使用Webhook模式
            logger.info(f"使用Webhook模式: {WEBHOOK_URL}")
            self.app.run_webhook(
                listen="0.0.0.0",
                port=WEBHOOK_PORT,
                url_path=TELEGRAM_BOT_TOKEN,
                webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
            )
        else:
            # 使用轮询模式
            logger.info("使用轮询模式")
            self.app.run_polling(poll_interval=1.0)


if __name__ == "__main__":
    bot = OrderBot()
    bot.run()
