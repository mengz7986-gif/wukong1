from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def main_menu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="商品目录"), KeyboardButton(text="购物车"))
    builder.row(KeyboardButton(text="我的订单"), KeyboardButton(text="帮助"))
    return builder.as_markup(resize_keyboard=True, is_persistent=False, one_time_keyboard=False)


def product_list(products) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.button(text=f"{product['name']} · {product['price_cents'] / 100:.2f}元", callback_data=f"product:{product['id']}")
    builder.button(text="查看购物车", callback_data="cart:view")
    builder.adjust(1)
    return builder.as_markup()


def product_actions(product_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="加入购物车", callback_data=f"cart:add:{product_id}")
    builder.button(text="返回目录", callback_data="catalog")
    builder.adjust(1)
    return builder.as_markup()


def cart_actions() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="提交订单", callback_data="checkout:start")
    builder.button(text="清空购物车", callback_data="cart:clear")
    builder.button(text="继续购物", callback_data="catalog")
    builder.adjust(1)
    return builder.as_markup()
