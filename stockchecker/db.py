import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")

# 既存 DB に後から追加した列 (起動時に自動で追加する)
MIGRATIONS = {
    "price_observations": {"vendor": "TEXT", "url": "TEXT", "detail": "TEXT",
                           "job_id": "INTEGER", "created_by": "INTEGER"},
    "materials": {"watch_urls": "TEXT",
                  "confirm_interval_days": "INTEGER",   # 価格・納期の確認周期 (NULL=既定)
                  "auto_confirm": "INTEGER NOT NULL DEFAULT 0",  # 期限到来で確認メールを自動送信
                  "last_confirmed_at": "TEXT",
                  "created_by": "INTEGER", "updated_by": "INTEGER", "updated_at": "TEXT"},
    "staff": {"password_hash": "TEXT", "role": "TEXT NOT NULL DEFAULT 'member'",
              "active": "INTEGER NOT NULL DEFAULT 1", "last_login_at": "TEXT"},
    "emails": {"answered_at": "TEXT", "followup_of": "INTEGER", "created_by": "INTEGER",
               "auto": "INTEGER NOT NULL DEFAULT 0", "message_id": "TEXT"},
    "alerts": {"ref_email_id": "INTEGER", "ref_order_id": "INTEGER"},
    "activity": {"actor": "TEXT", "client": "TEXT"},
    "comments": {"actor": "TEXT"},
    "email_materials": {"quantity": "INTEGER", "unit_price": "REAL", "requested_date": "TEXT",
                        "order_id": "INTEGER"},
    "fetch_jobs": {"started_by_name": "TEXT"},
}


def connect(path):
    conn = sqlite3.connect(path, detect_types=0, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(conn):
    conn.execute("PRAGMA journal_mode = WAL")  # 複数人が同時に使っても読み書きが詰まらないように
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                 (key, None if value is None else str(value)))
    conn.commit()


def log_activity(conn, user_id, action, material_id=None, detail=None, actor=None, client=None):
    """操作履歴を記録する。actor は操作者の名前 (未指定なら担当者名、どちらも無ければ「自動」)。"""
    if actor is None:
        r = conn.execute("SELECT name FROM staff WHERE id=?", (user_id,)).fetchone() if user_id else None
        actor = r["name"] if r else "自動"
    conn.execute("INSERT INTO activity (user_id, material_id, action, detail, actor, client) "
                 "VALUES (?,?,?,?,?,?)", (user_id, material_id, action, detail, actor, client))
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
                    currency="JPY", observed_at=None, vendor=None, url=None, detail=None,
                    job_id=None, created_by=None):
    cols = ["material_id", "source", "unit_price", "lead_time_days", "supplier_id",
            "stock_qty", "min_order_qty", "currency", "vendor", "url", "detail",
            "job_id", "created_by"]
    vals = [material_id, source, unit_price, lead_time_days, supplier_id,
            stock_qty, min_order_qty, currency, vendor, url, detail, job_id, created_by]
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
