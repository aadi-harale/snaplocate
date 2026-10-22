"""Thin query layer over the orders table."""
import sqlite3


class QueryTimeout(Exception):
    pass


def connect(path="shop.db"):
    return sqlite3.connect(path, timeout=5)


def fetch_orders(conn, since, until):
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, total, placed_at FROM orders "
            "WHERE placed_at BETWEEN ? AND ?", (since, until))
    except sqlite3.OperationalError:
        raise QueryTimeout("The report took too long to build")
    return cur.fetchall()
