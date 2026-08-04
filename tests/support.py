"""SQLite fixtures shared by the tests."""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: A join whose select list repeats id, name and created_at.
JOIN_QUERY = """
SELECT c.*, o.*
FROM customers c
JOIN orders o ON o.customer_id = c.id
"""

#: The same join with an unambiguous paging key.
KEYED_JOIN_QUERY = """
SELECT o.id AS order_id, c.*, o.*
FROM customers c
JOIN orders o ON o.customer_id = c.id
"""


def make_source(path: str, customers: int = 20, orders_each: int = 5) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT,
            created_at TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER,
            name TEXT,
            amount REAL,
            created_at TEXT
        );
        """
    )
    order_id = 0
    for customer_id in range(1, customers + 1):
        connection.execute(
            "INSERT INTO customers VALUES (?, ?, ?)",
            (customer_id, f"customer-{customer_id}", "2026-01-01"),
        )
        for _ in range(orders_each):
            order_id += 1
            connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
                (order_id, customer_id, f"order-{order_id}", order_id * 1.5, "2026-02-02"),
            )
    connection.commit()
    return connection


def make_target(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path)


def table_columns(connection: sqlite3.Connection, table: str) -> list:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def table_types(connection: sqlite3.Connection, table: str) -> dict:
    return {row[1]: row[2] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def row_count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
