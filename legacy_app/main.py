#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram自动下单机器人启动器

这个脚本提供了多种运行模式：
1. 运行Telegram Bot
2. 启动FastAPI后端服务
3. 同时启动 Bot 和 API
4. 测试订单解析
"""

import sys
import argparse
import subprocess
import os


def _configure_console_encoding() -> None:
    """Keep Chinese/emoji startup diagnostics printable on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure:
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                pass


_configure_console_encoding()


def run_bot():
    """运行Telegram Bot"""
    from bot import OrderBot
    bot = OrderBot()
    bot.run()


def run_api():
    """启动FastAPI后端服务"""
    from api import app, init_data
    import uvicorn
    import os
    from config import API_HOST, API_PORT

    print("📡 初始化数据...")
    init_data()

    print(f"🚀 启动FastAPI服务...")
    print(f"   API文档: http://{API_HOST}:{API_PORT}/docs")
    print(f"   管理后台: http://{API_HOST}:{API_PORT}/admin")
    print(f"=" * 60)

    uvicorn.run(
        "api:app",
        host=API_HOST,
        port=API_PORT,
        reload=os.getenv('API_RELOAD', '0') == '1',
        proxy_headers=False,
        log_level="info"
    )


def run_all():
    """同时启动 Bot 和 API"""
    print("⚠️  注意：同时启动 Bot 和 API 需要在两个终端中分别运行")
    print("")
    print("终端1 - 启动API:")
    print("  python main.py api")
    print("")
    print("终端2 - 启动Bot:")
    print("  python main.py bot")
    print("")
    print("或者使用进程管理器（如 pm2, supervisor）来管理这两个服务")


def test_parser():
    """测试订单解析"""
    from order_parser import OrderParser

    test_messages = [
        """
商品: 苹果 x2, 香蕉 x1
地址: 北京市朝阳区建国路88号
收件人: 张三
电话: 13800138000
        """,
        """
苹果3个, 香蕉2个, 橙子5个
地址: 上海市浦东新区世纪大道100号
收件人: 李四
手机: 13912345678
        """
    ]

    print("🔍 测试订单解析\n")
    for i, message in enumerate(test_messages, 1):
        print(f"测试用例 {i}:")
        print(f"内容: {message.strip()}")
        order_info = OrderParser.extract_order_info(message)

        if order_info:
            print("✅ 解析成功:")
            print(f"  商品: {order_info['items']}")
            print(f"  地址: {order_info['address']}")
            print(f"  收件人: {order_info['recipient']}")
            print(f"  电话: {order_info['phone']}")

            valid, reason = OrderParser.validate_order(order_info)
            print(f"  验证: {'✅ 通过' if valid else '❌ 失败'} - {reason}")
        else:
            print("❌ 解析失败")
        print()


def run_demo():
    """初始化演示数据（仓库/商品/库存/测试用户）"""
    print("🧩 初始化演示数据...")
    import init_demo_data
    # 直接执行脚本主逻辑
    warehouses = init_demo_data.init_warehouses()
    products = init_demo_data.init_products(warehouses)
    init_demo_data.init_warehouse_stock(warehouses, products)
    init_demo_data.init_demo_users()
    init_demo_data.show_summary()


def check_api():
    """检查API连接"""
    import requests
    from config import API_BASE_URL

    try:
        response = requests.get(f"{API_BASE_URL}/admin/stats", timeout=5)
        if response.status_code == 200:
            print("✅ API服务器运行正常")
        else:
            print(f"⚠️  API服务器返回状态码: {response.status_code}")
    except requests.exceptions.ConnectionError:
        print("❌ 无法连接到API服务器")
        print(f"   请确保API服务器已启动: python main.py api")
        return False
    except Exception as e:
        print(f"❌ 检查API时出错: {str(e)}")
        return False

    return True


def setup():
    """安装依赖并配置"""
    print("⚙️  检查并安装依赖...")
    try:
        # 安装依赖
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt'])
        print("✅ 依赖安装完成")
    except Exception as e:
        print(f"❌ 依赖安装失败: {str(e)}")
        return

    print("\n📝 配置说明:")

    # 检查 config.py
    from config import (
        TELEGRAM_BOT_TOKEN,
        API_SECRET_KEY,
        DEEPSEEK_ENABLED,
        DEEPSEEK_API_KEY,
        DEEPSEEK_BASE_URL,
        DEEPSEEK_MODEL,
    )

    if TELEGRAM_BOT_TOKEN in {'', 'YOUR_BOT_TOKEN_HERE', 'TOKEN', 'BOT_TOKEN'}:
        print("1. 需要配置 BOT_TOKEN（兼容 TELEGRAM_BOT_TOKEN）:")
        print("   - 在Telegram中搜索 @BotFather")
        print("   - 使用 /newbot 命令创建机器人")
        print("   - 将获得的 Token 写入 .env 的 BOT_TOKEN")
    else:
        print("✅ BOT_TOKEN 已配置")

    if API_SECRET_KEY in {'', 'API_SECRET_KEY', 'your-secret-key-change-in-production'}:
        print("2. 需要设置 API_SECRET_KEY (用于 API 安全)")

    if DEEPSEEK_ENABLED:
        if DEEPSEEK_API_KEY:
            print(f"✅ DeepSeek 已启用: {DEEPSEEK_BASE_URL} / {DEEPSEEK_MODEL}")
        else:
            print("3. DEEPSEEK_ENABLED=1，但 DEEPSEEK_API_KEY 为空")
    else:
        print("3. DeepSeek 当前未启用（设置 DEEPSEEK_ENABLED=1 后生效）")

    print("\n🚀 启动步骤:")
    print("   1. 启动API服务: python main.py api")
    print("   2. 启动Bot: python main.py bot")
    print("\n   或使用: python main.py all 查看更多信息")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Telegram自动下单机器人 - 前后端分离版',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
运行模式:
  api     - 启动FastAPI后端服务 (提供REST API和管理界面)
  bot     - 启动Telegram Bot
  all     - 查看如何同时启动 Bot 和 API
  test    - 测试订单解析功能
  setup   - 安装依赖并检查配置
  check   - 检查API连接
  demo    - 初始化演示仓库/商品/库存

示例:
  python main.py api       # 启动API服务
  python main.py bot       # 启动Telegram Bot
  python main.py setup     # 首次运行，安装依赖
  python main.py check     # 检查API连接
        """
    )

    parser.add_argument(
        'command',
        nargs='?',
        default='setup',
        choices=['api', 'bot', 'all', 'test', 'setup', 'check', 'demo'],
        help='运行模式 (默认: setup)'
    )

    args = parser.parse_args()

    if args.command == 'api':
        run_api()

    elif args.command == 'bot':
        if not check_api():
            print("\n❌ 请先启动API服务: python main.py api")
            sys.exit(1)
        print("🚀 启动Telegram Bot...")
        run_bot()

    elif args.command == 'all':
        run_all()

    elif args.command == 'test':
        print("🧪 测试订单解析功能...\n")
        test_parser()

    elif args.command == 'setup':
        setup()

    elif args.command == 'demo':
        run_demo()
    elif args.command == 'check':
        print("🔍 检查系统状态...\n")
        check_api()


if __name__ == '__main__':
    main()

