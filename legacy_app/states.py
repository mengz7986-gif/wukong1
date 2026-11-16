from aiogram.fsm.state import State, StatesGroup


class Checkout(StatesGroup):
    customer_name = State()
    phone = State()
    address = State()


class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()
    stock = State()

