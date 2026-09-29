import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path):
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(price_observations)")}
    for col in ("vendor", "url", "detail"):
        if col not in cols:
            conn.execute(f"ALTER TABLE price_observations ADD COLUMN {col} TEXT")
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(materials)")}
    if "watch_urls" not in mcols:
        conn.execute("ALTER TABLE materials ADD COLUMN watch_urls TEXT")
    conn.commit()


def latest_observation(conn, material_id):
    return conn.execute(
        "SELECT * FROM price_observations WHERE material_id = ? "
        "ORDER BY observed_at DESC, id DESC LIMIT 1",
        (material_id,),
    ).fetchone()


def previous_observation(conn, material_id, before_id):
    return conn.execute(
        "SELECT * FROM price_observations WHERE material_id = ? AND id < ? "
        "AND unit_price IS NOT NULL ORDER BY observed_at DESC, id DESC LIMIT 1",
        (material_id, before_id),
    ).fetchone()


def add_observation(conn, material_id, source, unit_price=None, lead_time_days=None,
                    supplier_id=None, stock_qty=None, min_order_qty=None,
                    currency="JPY", observed_at=None, vendor=None, url=None, detail=None):
    cols = ["material_id", "source", "unit_price", "lead_time_days", "supplier_id",
            "stock_qty", "min_order_qty", "currency", "vendor", "url", "detail"]
    vals = [material_id, source, unit_price, lead_time_days, supplier_id,
            stock_qty, min_order_qty, currency, vendor, url, detail]
    if observed_at:
        cols.append("observed_at")
        vals.append(observed_at)
    cur = conn.execute(
        f"INSERT INTO price_observations ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    conn.commit()
    return cur.lastrowid
