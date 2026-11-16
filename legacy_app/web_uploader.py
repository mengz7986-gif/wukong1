"""
[DEPRECATED] 旧 Web 上传模块（已废弃，请使用 FastAPI api.py）
保留仅用于兼容参考，主流程不要再调用本文件。
"""
import requests
import json
import logging
from typing import Dict, Optional, List, Tuple
from datetime import datetime
from config import WEB_UPLOAD_URL, WEB_UPLOAD_SECRET

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class WebUploader:
    """订单上传器 - 将订单上传到web服务器"""

    @staticmethod
    def upload_order(order_info: Dict) -> Tuple[bool, str]:
        """
        上传订单到web服务器
        Returns: (是否成功, 消息)
        """
        try:
            # 准备订单数据
            order_data = {
                "secret": WEB_UPLOAD_SECRET,
                "order": {
                    "id": order_info.get("id"),
                    "user_id": order_info.get("user_id"),
                    "items": [
                        {
                            "name": item[0],
                            "quantity": item[1],
                            "price": order_info.get("item_prices", {}).get(item[0], 0)
                        }
                        for item in order_info.get("items", [])
                    ],
                    "total_price": order_info.get("total_price"),
                    "address": order_info.get("address"),
                    "recipient": order_info.get("recipient"),
                    "phone": order_info.get("phone"),
                    "status": order_info.get("status", "pending"),
                    "created_at": order_info.get("created_at", datetime.now().isoformat())
                }
            }

            # 发送POST请求
            response = requests.post(
                WEB_UPLOAD_URL,
                json=order_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "TelegramBot/1.0"
                },
                timeout=10
            )

            # 检查响应
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    logger.info(f"订单 {order_info.get('id')} 上传成功")
                    return True, result.get("message", "订单上传成功")
                else:
                    logger.error(f"订单上传失败: {result.get('message')}")
                    return False, result.get("message", "订单上传失败")
            else:
                logger.error(f"HTTP错误: {response.status_code}")
                return False, f"HTTP错误: {response.status_code}"

        except requests.exceptions.Timeout:
            logger.error("上传超时")
            return False, "上传超时，请稍后重试"

        except requests.exceptions.ConnectionError:
            logger.error("连接错误")
            return False, "无法连接到服务器"

        except json.JSONDecodeError:
            logger.error("服务器返回数据格式错误")
            return False, "服务器返回数据格式错误"

        except Exception as e:
            logger.error(f"上传订单时出错: {str(e)}")
            return False, f"上传失败: {str(e)}"

    @staticmethod
    def check_webhook_available() -> bool:
        """检查webhook服务器是否可用"""
        try:
            # 尝试访问webhook URL
            base_url = WEB_UPLOAD_URL.rstrip('/').replace('/api/orders', '')
            response = requests.get(
                f"{base_url}/health",
                timeout=5,
                headers={"User-Agent": "TelegramBot/1.0"}
            )
            return response.status_code == 200
        except:
            return False


class WebServer:
    """简单的web服务器，用于接收订单"""

    def __init__(self, host: str = "0.0.0.0", port: int = 5000):
        self.host = host
        self.port = port
        self.secret = WEB_UPLOAD_SECRET
        self.orders = []
        self.app = None

    def start(self):
        """启动web服务器"""
        try:
            from flask import Flask, request, jsonify
            from flask.logging import default_handler
            import logging

            self.app = Flask(__name__)

            # 禁用Flask的默认日志
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)
            self.app.logger.removeHandler(default_handler)

            @self.app.route('/health', methods=['GET'])
            def health_check():
                return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

            @self.app.route('/api/orders', methods=['POST'])
            def receive_order():
                """接收订单"""
                try:
                    data = request.json

                    # 验证密钥
                    if data.get("secret") != self.secret:
                        return jsonify({
                            "success": False,
                            "message": "无效的密钥"
                        }), 403

                    order = data.get("order")
                    if not order:
                        return jsonify({
                            "success": False,
                            "message": "订单数据缺失"
                        }), 400

                    # 检查库存（这里可以连接真实的库存系统）
                    stock_available = True
                    insufficient_items = []

                    # 示例：模拟库存检查
                    for item in order.get("items", []):
                        # 这里可以连接真实的数据库或API检查库存
                        if item["name"] == "苹果" and item["quantity"] > 100:
                            stock_available = False
                            insufficient_items.append(item["name"])

                    if not stock_available:
                        return jsonify({
                            "success": False,
                            "message": f"库存不足: {', '.join(insufficient_items)}"
                        }), 400

                    # 保存订单
                    order_id = len(self.orders) + 1
                    order["server_id"] = order_id
                    order["received_at"] = datetime.now().isoformat()
                    self.orders.append(order)

                    logger.info(f"收到新订单: {order}")

                    # 这里可以：
                    # 1. 发送通知到管理员
                    # 2. 连接ERP系统
                    # 3. 生成物流单号
                    # 4. 发送确认邮件/短信

                    return jsonify({
                        "success": True,
                        "message": f"订单已接收，订单号: {order.get('id')}",
                        "order_id": order_id
                    })

                except Exception as e:
                    logger.error(f"处理订单时出错: {str(e)}")
                    return jsonify({
                        "success": False,
                        "message": f"服务器错误: {str(e)}"
                    }), 500

            @self.app.route('/api/orders', methods=['GET'])
            def list_orders():
                """查看所有订单"""
                secret = request.args.get("secret")
                if secret != self.secret:
                    return jsonify({
                        "success": False,
                        "message": "无效的密钥"
                    }), 403

                return jsonify({
                    "success": True,
                    "orders": self.orders
                })

            logger.info(f"Web服务器启动: http://{self.host}:{self.port}")
            logger.info(f"API地址: http://{self.host}:{self.port}/api/orders")
            self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)

        except ImportError:
            logger.error("Flask未安装，无法启动web服务器")
            print("请先安装Flask: pip install flask")


if __name__ == "__main__":
    # 测试上传功能
    test_order = {
        "id": 1,
        "user_id": "user123",
        "items": [("苹果", 2), ("香蕉", 3)],
        "item_prices": {"苹果": 5.0, "香蕉": 3.0},
        "total_price": 19.0,
        "address": "北京市朝阳区xxx街道",
        "recipient": "张三",
        "phone": "13800138000",
        "status": "pending"
    }

    success, msg = WebUploader.upload_order(test_order)
    print(f"上传结果: {'成功' if success else '失败'}")
    print(f"消息: {msg}")
