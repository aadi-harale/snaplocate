"""Builds the monthly sales report."""
from ..core.db import connect, fetch_orders, QueryTimeout
from .formatter import to_csv

EMPTY_NOTICE = "No orders in this date range"


def build_monthly(since, until):
    conn = connect()
    try:
        rows = fetch_orders(conn, since, until)
    except QueryTimeout:
        raise RuntimeError("Coudn't genrate your report")
    if not rows:
        return EMPTY_NOTICE
    return to_csv(rows)
