import os
import sqlite3
import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DEMAND2DEAL_DB", str(BASE_DIR / "d2d_history.db"))


def get_connection(path: str | None = None):
    path = path or DB_PATH
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db(path: str | None = None):
    path = path or DB_PATH
    dbp = Path(path)
    if not dbp.parent.exists():
        try:
            dbp.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    con = get_connection(path)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            product TEXT,
            quantity INTEGER,
            supplier TEXT,
            product_url TEXT,
            status TEXT,
            details TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            razorpay_order_id TEXT,
            razorpay_payment_id TEXT,
            razorpay_signature TEXT,
            verified INTEGER,
            details TEXT
        )
        """
    )
    con.commit()
    con.close()


def record_purchase(product: str, quantity: int, supplier: str, product_url: str | None = None, status: str = "UNKNOWN", details: dict | None = None, path: str | None = None) -> int:
    init_db(path)
    con = get_connection(path)
    cur = con.cursor()
    created_at = datetime.utcnow().isoformat()
    payload = dict(details or {})
    if "created_at" not in payload:
        payload["created_at"] = created_at
    if "correlation_id" not in payload:
        payload["correlation_id"] = f"purchase-{created_at}"
    cur.execute(
        "INSERT INTO purchases (created_at, product, quantity, supplier, product_url, status, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (created_at, product, int(quantity or 0), supplier, product_url or "", status, json.dumps(payload)),
    )
    con.commit()
    rowid = cur.lastrowid
    con.close()
    return rowid


def record_payment(razorpay_order_id: str | None, razorpay_payment_id: str | None, razorpay_signature: str | None, verified: bool, details: dict | None = None, path: str | None = None) -> int:
    init_db(path)
    con = get_connection(path)
    cur = con.cursor()
    created_at = datetime.utcnow().isoformat()
    payload = dict(details or {})
    if "created_at" not in payload:
        payload["created_at"] = created_at
    if "correlation_id" not in payload:
        payload["correlation_id"] = f"payment-{created_at}"
    cur.execute(
        "INSERT INTO payments (created_at, razorpay_order_id, razorpay_payment_id, razorpay_signature, verified, details) VALUES (?, ?, ?, ?, ?, ?)",
        (created_at, razorpay_order_id or "", razorpay_payment_id or "", razorpay_signature or "", 1 if verified else 0, json.dumps(payload)),
    )
    con.commit()
    rowid = cur.lastrowid
    con.close()
    return rowid


def get_purchases(limit: int = 100, path: str | None = None) -> list:
    init_db(path)
    con = get_connection(path)
    cur = con.cursor()
    cur.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def get_payments(limit: int = 100, path: str | None = None) -> list:
    init_db(path)
    con = get_connection(path)
    cur = con.cursor()
    cur.execute("SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def clear_history(path: str | None = None) -> None:
    init_db(path)
    con = get_connection(path)
    cur = con.cursor()
    cur.execute("DELETE FROM purchases")
    cur.execute("DELETE FROM payments")
    con.commit()
    con.close()


# Ensure DB exists on import
init_db()
