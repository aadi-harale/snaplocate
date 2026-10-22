"""Turns order rows into a downloadable CSV."""
import csv
import io

HEADERS = ["Order ID", "Total", "Placed at"]


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
