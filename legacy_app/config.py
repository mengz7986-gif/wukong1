import os
from typing import Dict, List

try:
    from dotenv import load_dotenv
except ImportError:  # Keep source imports usable before dependency installation.
    load_dotenv = None

if load_dotenv:
    load_dotenv()


def _env_first(*names: str, default: str = '') -> str:
    """Return the first non-empty environment variable from ``names``."""
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _env_ids(*names: str) -> List[int]:
    raw = _env_first(*names, default='')
    return [int(value) for value in raw.split(',') if value.strip().isdigit()]


def _env_chat_ids(*names: str) -> List[int]:
    """Parse Telegram chat IDs, including negative group/supergroup IDs."""
    raw = _env_first(*names, default='')
    result = []
    for value in raw.split(','):
        value = value.strip()
        try:
            if value:
                result.append(int(value))
        except ValueError:
            continue
    return result


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an operational concurrency limit without permitting an unbounded value."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


# Candidate/runtime knobs only; loading these values does not alter production.
ORDER_MESSAGE_WORKERS = _bounded_int('ORDER_MESSAGE_WORKERS', 8, 1, 16)
ORDER_CALLBACK_WORKERS = _bounded_int('ORDER_CALLBACK_WORKERS', 2, 0, 4)
AI_CONCURRENCY_LIMIT = _bounded_int('AI_CONCURRENCY_LIMIT', 4, 1, 8)

# Bounded send-client deadlines; polling remains on its existing separate client.
TELEGRAM_SEND_CONNECT_TIMEOUT = _bounded_int('TELEGRAM_SEND_CONNECT_TIMEOUT', 10, 1, 30)
TELEGRAM_SEND_READ_TIMEOUT = _bounded_int('TELEGRAM_SEND_READ_TIMEOUT', 15, 1, 30)
TELEGRAM_SEND_WRITE_TIMEOUT = _bounded_int('TELEGRAM_SEND_WRITE_TIMEOUT', 15, 1, 30)
TELEGRAM_SEND_POOL_TIMEOUT = _bounded_int('TELEGRAM_SEND_POOL_TIMEOUT', 1, 1, 5)

def telegram_send_request():
    from telegram.request import HTTPXRequest
    return HTTPXRequest(connection_pool_size=256,connect_timeout=TELEGRAM_SEND_CONNECT_TIMEOUT,
                        read_timeout=TELEGRAM_SEND_READ_TIMEOUT,write_timeout=TELEGRAM_SEND_WRITE_TIMEOUT,
                        pool_timeout=TELEGRAM_SEND_POOL_TIMEOUT)

# Telegram Bot Token (从 @BotFather 获取)。BOT_TOKEN 是新配置名，
# TELEGRAM_BOT_TOKEN 保留为旧版本兼容别名。
BOT_TOKEN = _env_first('BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', default='TOKEN')
TELEGRAM_BOT_TOKEN = BOT_TOKEN
# Telegram Payments provider token，当前订单默认人工收账，保留接口供支付接入。
PAYMENT_TOKEN = _env_first('PAYMENT_TOKEN', default='PAYMENT_TOKEN')

# Webhook配置
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')  # 例如: https://yourdomain.com/webhook
WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', 8443))

# API 配置 (FastAPI 后端)
API_HOST = os.getenv('API_HOST', '0.0.0.0')
API_PORT = int(os.getenv('API_PORT', 8000))
API_SECRET_KEY = _env_first('API_SECRET_KEY', default='API_SECRET_KEY')

# TG 自动停售规则：
# - zero：总库存 <= 0 才自动 TG 停售（下单机器人）
# - threshold：总库存 <= 少量预警值 自动 TG 停售（批发机器人）
STOCK_AUTO_UNLIST_MODE = os.getenv('STOCK_AUTO_UNLIST_MODE', 'threshold').strip().lower()

# Bot 档位：
# - order：普通下单机器人
# - wholesale：批发机器人，仅启用批发端专属菜单和文案
BOT_PROFILE = os.getenv('BOT_PROFILE', 'order').strip().lower()
IS_WHOLESALE_BOT = BOT_PROFILE in ('wholesale', 'batch', 'dealer')

WHOLESALE_PRIVATE_WAREHOUSE_NAME = os.getenv(
    'WHOLESALE_PRIVATE_WAREHOUSE_NAME', '旺财仓',
).strip()
WHOLESALE_PRIVATE_USERNAME = os.getenv(
    'WHOLESALE_PRIVATE_USERNAME', 'xiaolin005',
).strip().lstrip('@').casefold()

# 后台管理账号密码（浏览器登录）
ADMIN_USERNAME = _env_first('ADMIN_USERNAME', default='admin')
ADMIN_PASSWORD = _env_first('ADMIN_PASSWORD', default='ADMIN_PASSWORD')
SESSION_SECRET_KEY = _env_first('SESSION_SECRET_KEY', default='SESSION_SECRET_KEY')

# API 基础 URL (供 Telegram Bot 调用)
API_BASE_URL = os.getenv('API_BASE_URL', f'http://localhost:{API_PORT}')

# DeepSeek 智能订单识别（仅用于复杂文本解析，不直接创建订单）
DEEPSEEK_ENABLED = os.getenv('DEEPSEEK_ENABLED', '0').strip().lower() in (
    '1', 'true', 'yes', 'on'
)
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '').strip()
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash').strip()
DEEPSEEK_VISION_MODEL = os.getenv('DEEPSEEK_VISION_MODEL', DEEPSEEK_MODEL).strip()
IMAGE_ORDER_RECOGNITION_ENABLED = _env_first(
    'IMAGE_ORDER_RECOGNITION_ENABLED', default='0'
).lower() in ('1', 'true', 'yes', 'on')
try:
    DEEPSEEK_TIMEOUT = max(3, int(os.getenv('DEEPSEEK_TIMEOUT', '12')))
except (TypeError, ValueError):
    DEEPSEEK_TIMEOUT = 12
try:
    DEEPSEEK_MIN_CONFIDENCE = min(
        1.0,
        max(0.0, float(os.getenv('DEEPSEEK_MIN_CONFIDENCE', '0.75'))),
    )
except (TypeError, ValueError):
    DEEPSEEK_MIN_CONFIDENCE = 0.75

# 管理员 Telegram ID（用于入库/图片更新权限）。多个用逗号分隔，例如: 123,456
# 为空时默认允许所有人（便于本地测试）
# 售后 / 补发单推送给客服的 Telegram 数字 ID（不是用户名——机器人无法靠用户名
# 主动发消息）。客服必须先和机器人对话过一次，否则推送会失败。
# 留空时回退到 ADMIN_TELEGRAM_IDS。
# 一键停仓：全局暂停接单。由后台网页开关写入 settings.json，
# 每次下单预览前都会重新读取（见 bot._build_order_preview 开头的 load_runtime_settings）。
# 停仓只挡「新订单进入」，不影响已生成订单的发货、打单和售后。
SALES_PAUSED = os.getenv('SALES_PAUSED', '0').strip().lower() in (
    '1', 'true', 'yes', 'on'
)
SALES_PAUSED_MESSAGE = os.getenv(
    'SALES_PAUSED_MESSAGE',
    '🛑 当前已暂停接单\n\n仓库正在盘点/补货，暂时无法下单。\n开仓后会第一时间通知，感谢理解 🙏',
).strip()
SALES_PAUSED_AT = ''
# 按仓库停仓的原始开关（settings.json 的 sales 段）。解释规则见 sales_pause.py，
# 不要在别处再写一套判断 —— 停售判断散成三处的坑已经踩过一次了。
SALES_PAUSE_SECTION = {}

# 数量 / 自动分单规则：同一地址不限制总支数，每个正式子单最多 6 支。
# 旧的上限变量继续保留给历史配置读取，但默认不再拒单。
_DEFAULT_MAX_UNITS_PER_ORDER = '0'
_DEFAULT_UNITS_PER_PACKAGE = '6'
_DEFAULT_ADDRESS_DAILY_MAX_UNITS = '0'
SPLIT_NOTIFICATION_CHAT_ID = os.getenv('SPLIT_NOTIFICATION_CHAT_ID', '-1004408286513').strip()
try:
    MAX_UNITS_PER_ORDER = max(0, int(os.getenv(
        'MAX_UNITS_PER_ORDER', _DEFAULT_MAX_UNITS_PER_ORDER)))
except ValueError:
    MAX_UNITS_PER_ORDER = 0
try:
    UNITS_PER_PACKAGE = max(0, int(os.getenv(
        'UNITS_PER_PACKAGE', _DEFAULT_UNITS_PER_PACKAGE)))
except ValueError:
    UNITS_PER_PACKAGE = 0
try:
    ADDRESS_DAILY_MAX_UNITS = max(0, int(os.getenv(
        'ADDRESS_DAILY_MAX_UNITS', _DEFAULT_ADDRESS_DAILY_MAX_UNITS)))
except ValueError:
    ADDRESS_DAILY_MAX_UNITS = 0

try:
    ADDRESS_DAILY_MAX_ORDERS = max(0, int(os.getenv('ADDRESS_DAILY_MAX_ORDERS', '0')))
except ValueError:
    ADDRESS_DAILY_MAX_ORDERS = 0

try:
    ORDER_AUTO_SPLIT_UNITS = max(1, int(os.getenv('ORDER_AUTO_SPLIT_UNITS', '6')))
except ValueError:
    ORDER_AUTO_SPLIT_UNITS = 6
CATEGORY_SPLIT_LIMITS = {}

# 发货时的快递单号来源：
#   批发端（.87）后台手填，允许清空 —— 与波派机器人一致
#   下单端（.24）由风火递推送回来，绝不能被一次「标记发货」清掉
MANUAL_TRACKING_ON_SHIP = str(os.getenv(
    'MANUAL_TRACKING_ON_SHIP', '1' if IS_WHOLESALE_BOT else '0',
)).strip().lower() in ('1', 'true', 'yes', 'on')
# 下单机 .24 默认关闭：用户这次只要批发机开这个通知。
# 新订单客服通知（同步自波派机器人 2026-08-20）
# 机器人不能只凭用户名主动私聊，所以最终一定要数字 chat_id：
#   ORDER_NOTIFICATION_TELEGRAM_IDS 显式配 > 按数据库用户名解析 > 退到管理员
ORDER_NOTIFY_ENABLED = str(os.getenv(
    'ORDER_NOTIFY_ENABLED', '0',
)).strip().lower() in ('1', 'true', 'yes', 'on')
ORDER_NOTIFICATION_TELEGRAM_IDS = [
    x.strip() for x in os.getenv('ORDER_NOTIFICATION_TELEGRAM_IDS', '').split(',')
    if x.strip()
]
ORDER_NOTIFICATION_USERNAMES = [
    x.strip().lstrip('@').lower()
    for x in os.getenv('ORDER_NOTIFICATION_USERNAMES', '').split(',')
    if x.strip()
]

# 超过单笔容量而生成多个正式订单时，向这些群发送强提醒。留空时由 API
# 回退到新订单通知目标，再回退管理员 Telegram ID。
SPLIT_ORDER_GROUP_IDS = _env_chat_ids('SPLIT_ORDER_GROUP_IDS', 'SPLIT_ORDER_GROUP_ID')

# USDT 收款按实时 CNY 汇率入账。默认读取 Binance C2C 的 USDT/CNY 广告价；API
# 拉取失败时收款接口会明确失败，绝不静默使用代码内硬编码汇率。若运营有明确
# 的结算口径，可通过环境变量显式指定 USDT_CNY_RATE_OVERRIDE。
USDT_CNY_RATE_OVERRIDE = os.getenv('USDT_CNY_RATE_OVERRIDE', '').strip()
USDT_CNY_RATE_URL = os.getenv(
    'USDT_CNY_RATE_URL',
    'https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search',
).strip()
USDT_CNY_RATE_JSON_PATH = os.getenv('USDT_CNY_RATE_JSON_PATH', 'data.0.adv.price').strip()
USDT_CNY_RATE_TRADE_TYPE = os.getenv('USDT_CNY_RATE_TRADE_TYPE', 'BUY').strip().upper() or 'BUY'
try:
    USDT_CNY_RATE_TTL_SECONDS = max(5, int(os.getenv('USDT_CNY_RATE_TTL_SECONDS', '60')))
except ValueError:
    USDT_CNY_RATE_TTL_SECONDS = 60
try:
    USDT_CNY_RATE_TIMEOUT_SECONDS = max(1.0, float(os.getenv('USDT_CNY_RATE_TIMEOUT_SECONDS', '6')))
except ValueError:
    USDT_CNY_RATE_TIMEOUT_SECONDS = 6.0


AFTERSALE_NOTIFY_IDS = [
    int(x.strip()) for x in os.getenv('AFTERSALE_NOTIFY_IDS', '').split(',') if x.strip().isdigit()
]

ADMIN_ID = _env_first('ADMIN_ID', default='')
ADMIN_TELEGRAM_IDS = _env_ids('ADMIN_ID', 'ADMIN_TELEGRAM_IDS')
AGENT_GROUP_IDS = _env_chat_ids('AGENT_GROUP_IDS', 'AGENT_GROUP_ID')

# 产品配置 (商品名称: 价格)
PRODUCTS = {
    "苹果": 5.0,
    "香蕉": 3.0,
    "橙子": 4.0,
    "西瓜": 15.0,
    "葡萄": 8.0,
    "草莓": 12.0,
    "桃子": 6.0,
    "梨": 4.5,
}

# 初始库存
INITIAL_STOCK = {
    "苹果": 100,
    "香蕉": 150,
    "橙子": 120,
    "西瓜": 50,
    "葡萄": 80,
    "草莓": 60,
    "桃子": 90,
    "梨": 110,
}

# 用户初始余额 (用户ID: 余额)
INITIAL_BALANCES = {
    # 示例用户余额
    "user123": 100.0,
}

# 支持命令
COMMANDS = {
    'start': '开始 / 功能菜单',
    'help': '帮助与下单格式',
    'stock': '库存预览',
    'products': '单价查询',
    'order': '下单模板',
    'orders': '下单记录',
    'debt': '个人中心/欠款',
    'cancel': '申请取消未发货订单（等待后台审核）',
    'stats': '今日发货/补货统计（管理员）',
}

# 订单格式示例（自动匹配仓库）
ORDER_FORMAT_EXAMPLE = """📋 下单格式示例:

<b>基本格式:</b>
商品: 苹果 x2, 香蕉 x1, 橙子 x3
地址: 北京市朝阳区建国路88号
收件人: 张三
电话: 13800138000

<b>简略格式（省略"商品:"标签）:</b>
苹果 x2, 香蕉 x1, 橙子 x3
地址: 北京市朝阳区建国路88号
收件人: 李四
电话: 13912345678

<b>每种格式一个数量:</b>
苹果2个, 香蕉1个, 橙子5个
地址: 上海市浦东新区世纪大道100号
收件人: 王五
电话: 13800138000

<b>混合格式:</b>
购买: 苹果 x2, 香蕉1个, 橙子 5
地址: 广州市天河区天河路200号
收件人: 赵六
联系方式: 18612345678

✨ <b>系统特点:</b>
• 🔄 自动匹配最优仓库（库存最充足）
• 🚚 自动计算各仓库运费
• 💡 运费会从不同仓库分别收取
• ⚠️ 如果某个商品在所有仓库库存都不足，订单将失败
"""

# 入库格式示例
INBOUND_FORMAT_EXAMPLE = """📥 到货入库格式示例:

<b>命令方式:</b>
/inbound 苹果 50
/inbound 苹果 50 北京仓库
/inbound 苹果 +50 北京仓库
/inbound 苹果 =100 北京仓库

<b>文本方式:</b>
入库: 苹果 +50, 香蕉 +20
仓库: 北京仓库

说明:
• +数量 / 数量 = 增量入库
• =数量 = 覆盖库存为指定值
• 不写仓库时使用默认仓库
"""



# 下单说明（面向客户）
ORDER_GUIDE = """🧾 <b>输入栏快速下单模板</b>

<code>下单
北京市朝阳区xxx街道xxx号
张三
19999999999
品类(鸭子)口味(苹果)x1
品类(鸭子)口味(西瓜)x2
品类(板砖)口味(苹果)x1</code>

<b>注意格式：</b>
第一行：下单
第二行：收货地址
第三行：联系人姓名
第四行：手机号码
其后每行：品类(型号)口味(口味)x数量

系统会自动解析：地址 / 姓名 / 手机号 / 品类 + 口味 + 数量。
把单子粘贴发送给我，确认后生成订单。
也支持地址、姓名、电话、商品粘在同一行，或口味和数量连续发送；
系统会先整理成标准确认单，核对无误后点击“确认下单”。
也可用下方按钮浏览商品分类 / 库存。
"""


# ==================== 运费规则 ====================
# 按商品分类/关键词累计数量计费：
# - 普通类：每 N 只/根 X 元
# - 烟弹类：每 N 颗 Y 元
_DEFAULT_SHIPPING_FEE_AMOUNT = '1' if IS_WHOLESALE_BOT else '6'
SHIPPING_FEE_AMOUNT = float(os.getenv('SHIPPING_FEE_AMOUNT', _DEFAULT_SHIPPING_FEE_AMOUNT))
_DEFAULT_SHIPPING_NORMAL_FEE = '0.8' if IS_WHOLESALE_BOT else str(SHIPPING_FEE_AMOUNT)
_DEFAULT_SHIPPING_POD_FEE = '0.5' if IS_WHOLESALE_BOT else str(SHIPPING_FEE_AMOUNT)
SHIPPING_NORMAL_FEE_AMOUNT = float(os.getenv('SHIPPING_NORMAL_FEE_AMOUNT', _DEFAULT_SHIPPING_NORMAL_FEE))
SHIPPING_POD_FEE_AMOUNT = float(os.getenv('SHIPPING_POD_FEE_AMOUNT', _DEFAULT_SHIPPING_POD_FEE))
_DEFAULT_SHIPPING_NORMAL_QTY = '1' if IS_WHOLESALE_BOT else '5'
_DEFAULT_SHIPPING_POD_QTY = '1' if IS_WHOLESALE_BOT else '10'
SHIPPING_NORMAL_QTY = int(os.getenv('SHIPPING_NORMAL_QTY', _DEFAULT_SHIPPING_NORMAL_QTY))
SHIPPING_POD_QTY = int(os.getenv('SHIPPING_POD_QTY', _DEFAULT_SHIPPING_POD_QTY))
SHIPPING_POD_KEYWORDS = [
    x.strip() for x in os.getenv('SHIPPING_POD_KEYWORDS', '烟弹,弹,pod,POD').split(',') if x.strip()
]

# 兼容旧逻辑：若未启用新规则，可回退仓库运费
SHIPPING_MODE = os.getenv('SHIPPING_MODE', 'qty_rule')  # qty_rule | warehouse

# ==================== 已退役的风火递兼容常量 ====================
# 仅防止历史模块在回滚/审计时导入失败。彩虹仓不再从环境变量或
# settings.json 读取风火递配置，也不提供可重新启用的开关。
FENGHUODI_ENABLED = False
FENGHUODI_PRINT_URL = ''
FENGHUODI_API_URL = ''
FENGHUODI_API_KEY = ''
FENGHUODI_SENDER_NAME = ''
FENGHUODI_SENDER_PHONE = ''
FENGHUODI_SENDER_ADDRESS = ''
FENGHUODI_PID = ''
FENGHUODI_PSECRET = ''
FENGHUODI_USER_ID = ''
FENGHUODI_MERCHANT_ID = ''
FENGHUODI_MERCHANT_NAME = ''
FENGHUODI_BASE_URL = ''


def load_runtime_settings(settings_path='settings.json'):
    """Load settings saved by the admin UI into the shared config module."""
    import json
    from pathlib import Path

    path = Path(settings_path)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        shipping = data.get('shipping') or {}
        sales = data.get('sales') or {}
        if sales:
            globals()['SALES_PAUSED'] = bool(sales.get('paused'))
            message = str(sales.get('message') or '').strip()
            if message:
                globals()['SALES_PAUSED_MESSAGE'] = message
            globals()['SALES_PAUSED_AT'] = str(sales.get('paused_at') or '')
        # 按仓库停仓：整段原样交给 sales_pause 解释，规则只有那一处实现。
        # 无论有没有 sales 段都要写，否则后台开仓后 Bot 还留着上一次的停仓状态。
        try:
            import sales_pause as _sales_pause
            globals()['SALES_PAUSE_SECTION'] = _sales_pause.normalize(sales)
        except Exception:
            globals()['SALES_PAUSE_SECTION'] = {}
        # 数量/包裹限额：后台可改（快递政策会变），改完 Bot 下一单就生效
        packaging = data.get('packaging') or {}
        if packaging:
            for _key, _name in (
                ('max_units_per_order', 'MAX_UNITS_PER_ORDER'),
                ('units_per_package', 'UNITS_PER_PACKAGE'),
                ('address_daily_max_units', 'ADDRESS_DAILY_MAX_UNITS'),
                ('address_daily_max_orders', 'ADDRESS_DAILY_MAX_ORDERS'),
            ):
                if _key in packaging:
                    try:
                        globals()[_name] = max(0, int(packaging.get(_key) or 0))
                    except (TypeError, ValueError):
                        pass
        # 新规则固定为同地址不限支数、每 6 支自动生成一笔正式子单。这样旧
        # settings.json 中的 10 支上限不会在升级后继续拒单。
        globals()['MAX_UNITS_PER_ORDER'] = 0
        try:
            default_split = max(1, int(packaging.get(
                'units_per_package', globals().get('ORDER_AUTO_SPLIT_UNITS', 6)
            ) or 6))
        except (TypeError, ValueError):
            default_split = 6
        globals()['ORDER_AUTO_SPLIT_UNITS'] = default_split
        globals()['UNITS_PER_PACKAGE'] = default_split
        try:
            import package_rules as _package_rules
            globals()['CATEGORY_SPLIT_LIMITS'] = _package_rules.normalize_category_limits(
                packaging.get('category_limits')
            )
        except Exception:
            globals()['CATEGORY_SPLIT_LIMITS'] = {}
        if shipping:
            globals()['SHIPPING_MODE'] = shipping.get('mode', SHIPPING_MODE)
            globals()['SHIPPING_FEE_AMOUNT'] = float(
                shipping.get('fee_amount', SHIPPING_FEE_AMOUNT)
            )
            globals()['SHIPPING_NORMAL_FEE_AMOUNT'] = float(
                shipping.get('normal_fee_amount', SHIPPING_NORMAL_FEE_AMOUNT)
            )
            globals()['SHIPPING_POD_FEE_AMOUNT'] = float(
                shipping.get('pod_fee_amount', SHIPPING_POD_FEE_AMOUNT)
            )
            globals()['SHIPPING_NORMAL_QTY'] = int(
                shipping.get('normal_qty', SHIPPING_NORMAL_QTY)
            )
            globals()['SHIPPING_POD_QTY'] = int(
                shipping.get('pod_qty', SHIPPING_POD_QTY)
            )
            if shipping.get('pod_keywords'):
                globals()['SHIPPING_POD_KEYWORDS'] = list(shipping['pod_keywords'])
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


# The API and Bot share settings.json; load it at startup as well as on demand.
load_runtime_settings()
