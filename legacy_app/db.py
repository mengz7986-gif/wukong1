from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        self._seed_products()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                total_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT '待处理',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                unit_price_cents INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id)
            );
            """
        )
        self.conn.commit()

    def _seed_products(self) -> None:
        if self.conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            return
        self.conn.executemany(
            "INSERT INTO products(name, description, price_cents, stock) VALUES (?, ?, ?, ?)",
            [
                ("标准套餐", "适合日常使用", 990, 99),
                ("进阶套餐", "包含更多服务内容", 1990, 50),
                ("定制服务", "下单后由客服确认细节", 4990, 10),
            ],
        )
        self.conn.commit()

    def upsert_user(self, telegram_id: int, username: str | None, first_name: str) -> None:
        self.conn.execute(
            """INSERT INTO users(telegram_id, username, first_name) VALUES (?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (telegram_id, username, first_name or "用户"),
        )
        self.conn.commit()

    def list_products(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id"))

    def get_product(self, product_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()

    def add_product(self, name: str, description: str, price_cents: int, stock: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO products(name, description, price_cents, stock) VALUES (?, ?, ?, ?)",
            (name, description, price_cents, stock),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def create_order(
        self,
        telegram_id: int,
        customer_name: str,
        phone: str,
        address: str,
        cart: dict[int, int],
    ) -> tuple[int, int, list[sqlite3.Row]]:
        if not cart:
            raise ValueError("购物车为空")
        ids = list(cart)
        marks = ",".join("?" for _ in ids)
        products = list(
            self.conn.execute(
                f"SELECT * FROM products WHERE active=1 AND id IN ({marks})", ids
            )
        )
        by_id = {int(row["id"]): row for row in products}
        total = 0
        for product_id, quantity in cart.items():
            row = by_id.get(int(product_id))
            if row is None:
                raise ValueError("商品不存在或已下架")
            if quantity < 1 or quantity > int(row["stock"]):
                raise ValueError(f"{row['name']} 库存不足")
            total += int(row["price_cents"]) * quantity
        try:
            self.conn.execute("BEGIN")
            cur = self.conn.execute(
                "INSERT INTO orders(telegram_id, customer_name, phone, address, total_cents) VALUES (?, ?, ?, ?, ?)",
                (telegram_id, customer_name, phone, address, total),
            )
            order_id = int(cur.lastrowid)
            for product_id, quantity in cart.items():
                row = by_id[int(product_id)]
                updated = self.conn.execute(
                    "UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                    (quantity, product_id, quantity),
                ).rowcount
                if updated != 1:
                    raise ValueError(f"{row['name']} 库存不足")
                self.conn.execute(
                    "INSERT INTO order_items(order_id, product_id, product_name, unit_price_cents, quantity) VALUES (?, ?, ?, ?, ?)",
                    (order_id, product_id, row["name"], row["price_cents"], quantity),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return order_id, total, products

    def list_user_orders(self, telegram_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
                (telegram_id, limit),
            )
        )

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def list_order_items(self, order_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)))

    def list_recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)))

    def set_order_status(self, order_id: int, status: str) -> bool:
        cur = self.conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        self.conn.commit()
        return cur.rowcount == 1

    def close(self) -> None:
        self.conn.close()


def money(cents: int) -> str:
    return f"{cents / 100:.2f} 元"

