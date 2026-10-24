"""SQLite storage. Plain sqlite3, no ORM, so the schema stays easy to inspect."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    order_date      TEXT,            -- ISO date (YYYY-MM-DD)
    total           REAL,            -- order total as shown in the confirmation email
    currency        TEXT,
    marketplace     TEXT,            -- amazon.ca, amazon.com ...
    buyer           TEXT,
    invoice_url     TEXT,            -- Amazon "print invoice" page for this order
    invoice_pdf     TEXT,            -- local path of the downloaded / generated invoice PDF
    source_message_id TEXT,
    subject         TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id     TEXT PRIMARY KEY,  -- Amazon shipmentId, or synthesized "<order>-S<n>"
    order_id        TEXT NOT NULL,
    shipped_date    TEXT,
    total           REAL,              -- amount charged for this shipment
    currency        TEXT,
    tracking_url    TEXT,
    source_message_id TEXT,
    source          TEXT NOT NULL DEFAULT 'email',   -- email | report
    card_last4      TEXT,              -- from the Amazon Business reconciliation report
    payer           TEXT,              -- "Account User" in the report
    payment_ref     TEXT,              -- "Payment Reference ID" in the report
    charge_code     TEXT,              -- "Charge Identifier": the code printed on the card statement (AMAZON* 5E66L7HX2)
    created_at      TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id       TEXT PRIMARY KEY,  -- synthesized "<order>-R<message id>"
    order_id        TEXT NOT NULL,
    refund_date     TEXT,
    total           REAL,
    currency        TEXT,
    source_message_id TEXT,
    source          TEXT NOT NULL DEFAULT 'email',
    card_last4      TEXT,
    charge_code     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL,
    shipment_id     TEXT,              -- set when the item appeared in a "Shipped" email
    description     TEXT,
    quantity        INTEGER,
    unit_price      REAL,
    currency        TEXT
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_date        TEXT NOT NULL,
    description     TEXT NOT NULL,
    amount          REAL,              -- positive = money out (charge), negative = money in (refund); NULL = unknown (date-only request)
    currency        TEXT,
    account         TEXT,
    source_file     TEXT,
    raw_row         TEXT,              -- JSON of the original CSV row
    fingerprint     TEXT UNIQUE,       -- de-duplication key
    is_amazon       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_txn_id     INTEGER NOT NULL UNIQUE,
    kind            TEXT NOT NULL,     -- shipment | order | refund | shipment_group
    ref_ids         TEXT NOT NULL,     -- JSON list of shipment/order/refund ids
    order_id        TEXT,
    method          TEXT NOT NULL,     -- exact_shipment | exact_order | shipment_group | manual
    confidence      REAL NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (bank_txn_id) REFERENCES bank_transactions(id)
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id      TEXT PRIMARY KEY,
    source          TEXT NOT NULL,     -- gmail | eml
    outcome         TEXT NOT NULL,     -- order | shipment | refund | ignored | error
    detail          TEXT,
    processed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL,
    path        TEXT NOT NULL UNIQUE,
    label       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns introduced after the first release to existing databases."""
    wanted = {
        "orders": {"invoice_pdf": "TEXT"},
        "shipments": {"source": "TEXT NOT NULL DEFAULT 'email'", "card_last4": "TEXT", "payer": "TEXT", "payment_ref": "TEXT", "charge_code": "TEXT"},
        "refunds": {"source": "TEXT NOT NULL DEFAULT 'email'", "card_last4": "TEXT", "charge_code": "TEXT"},
    }
    for table, cols in wanted.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()


@contextmanager
def session(path=None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key, value):
    conn.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------- orders / shipments / refunds ----------

def upsert_order(conn, o):
    """o is a parsers.amazon_email.ParsedOrder."""
    existing = conn.execute("SELECT * FROM orders WHERE order_id = ?", (o.order_id,)).fetchone()
    if existing:
        # Keep the earliest known order date and fill blanks; never overwrite a
        # known total with None.
        conn.execute(
            """UPDATE orders SET
                 order_date = COALESCE(MIN(order_date, ?), ?),
                 total = COALESCE(?, total),
                 currency = COALESCE(?, currency),
                 marketplace = COALESCE(?, marketplace),
                 buyer = COALESCE(?, buyer),
                 invoice_url = COALESCE(invoice_url, ?),
                 subject = COALESCE(?, subject)
               WHERE order_id = ?""",
            (o.order_date, o.order_date, o.total, o.currency, o.marketplace, o.buyer,
             o.invoice_url, o.subject, o.order_id),
        )
    else:
        conn.execute(
            """INSERT INTO orders(order_id, order_date, total, currency, marketplace, buyer,
                                  invoice_url, source_message_id, subject, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (o.order_id, o.order_date, o.total, o.currency, o.marketplace, o.buyer,
             o.invoice_url, o.source_message_id, o.subject, now_iso()),
        )
    if o.items:
        # Replace unassigned items for this order (shipment-level items are kept).
        conn.execute("DELETE FROM order_items WHERE order_id = ? AND shipment_id IS NULL", (o.order_id,))
        for it in o.items:
            conn.execute(
                "INSERT INTO order_items(order_id, shipment_id, description, quantity, unit_price, currency) VALUES (?,?,?,?,?,?)",
                (o.order_id, None, it.description, it.quantity, it.unit_price, it.currency),
            )


def ensure_order_stub(conn, order_id, marketplace=None, date=None, invoice_url=None):
    """Create a placeholder order row when a shipment/refund email arrives before (or without) the order email."""
    if not conn.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone():
        conn.execute(
            "INSERT INTO orders(order_id, order_date, total, currency, marketplace, invoice_url, created_at) VALUES (?,?,?,?,?,?,?)",
            (order_id, date, None, None, marketplace, invoice_url, now_iso()),
        )
    elif invoice_url:
        conn.execute("UPDATE orders SET invoice_url = COALESCE(invoice_url, ?) WHERE order_id = ?", (invoice_url, order_id))


def upsert_shipment(conn, s):
    ensure_order_stub(conn, s.order_id, s.marketplace, s.shipped_date, s.invoice_url)
    # The Amazon Business reconciliation report may already hold this charge
    # (same order, same amount, no email id yet). Merge instead of duplicating.
    if s.total is not None and not conn.execute("SELECT 1 FROM shipments WHERE shipment_id = ?", (s.shipment_id,)).fetchone():
        twin = conn.execute(
            """SELECT shipment_id FROM shipments WHERE order_id = ? AND source = 'report' AND source_message_id IS NULL
               AND ABS(total - ?) < 0.005 ORDER BY shipped_date LIMIT 1""", (s.order_id, s.total)).fetchone()
        if twin:
            conn.execute("UPDATE shipments SET source_message_id = ?, tracking_url = COALESCE(tracking_url, ?) WHERE shipment_id = ?",
                         (s.source_message_id, s.tracking_url, twin["shipment_id"]))
            conn.execute("DELETE FROM order_items WHERE shipment_id = ?", (twin["shipment_id"],))
            for it in s.items:
                conn.execute(
                    "INSERT INTO order_items(order_id, shipment_id, description, quantity, unit_price, currency) VALUES (?,?,?,?,?,?)",
                    (s.order_id, twin["shipment_id"], it.description, it.quantity, it.unit_price, it.currency))
            return
    conn.execute(
        """INSERT INTO shipments(shipment_id, order_id, shipped_date, total, currency, tracking_url, source_message_id, created_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(shipment_id) DO UPDATE SET
             shipped_date = excluded.shipped_date, total = COALESCE(excluded.total, total),
             currency = COALESCE(excluded.currency, currency), tracking_url = COALESCE(excluded.tracking_url, tracking_url)""",
        (s.shipment_id, s.order_id, s.shipped_date, s.total, s.currency, s.tracking_url, s.source_message_id, now_iso()),
    )
    conn.execute("DELETE FROM order_items WHERE shipment_id = ?", (s.shipment_id,))
    for it in s.items:
        conn.execute(
            "INSERT INTO order_items(order_id, shipment_id, description, quantity, unit_price, currency) VALUES (?,?,?,?,?,?)",
            (s.order_id, s.shipment_id, it.description, it.quantity, it.unit_price, it.currency),
        )


def upsert_refund(conn, r):
    ensure_order_stub(conn, r.order_id, r.marketplace, r.refund_date)
    conn.execute(
        """INSERT INTO refunds(refund_id, order_id, refund_date, total, currency, source_message_id, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(refund_id) DO UPDATE SET refund_date = excluded.refund_date, total = excluded.total""",
        (r.refund_id, r.order_id, r.refund_date, r.total, r.currency, r.source_message_id, now_iso()),
    )


def upsert_report_charge(conn, row) -> str:
    """Store one line of the Amazon Business reconciliation report. row is sources.amazon_report.ReportRow.
    Returns 'charge', 'refund' or 'merged'."""
    ensure_order_stub(conn, row.order_id, row.marketplace, row.order_date,
                      f"https://www.{row.marketplace}/gp/css/summary/print.html?orderID={row.order_id}")
    conn.execute("UPDATE orders SET order_date = COALESCE(order_date, ?), currency = COALESCE(currency, ?), buyer = COALESCE(buyer, ?) WHERE order_id = ?",
                 (row.order_date, row.currency, row.payer, row.order_id))
    code = getattr(row, "charge_code", None)
    if row.is_refund:
        conn.execute(
            """INSERT INTO refunds(refund_id, order_id, refund_date, total, currency, source, card_last4, charge_code, created_at)
               VALUES (?,?,?,?,?,'report',?,?,?)
               ON CONFLICT(refund_id) DO UPDATE SET refund_date = excluded.refund_date, total = excluded.total, card_last4 = excluded.card_last4,
                 charge_code = COALESCE(excluded.charge_code, refunds.charge_code)""",
            (row.payment_ref, row.order_id, row.txn_date, row.amount, row.currency, row.card_last4, code, now_iso()))
        return "refund"
    if conn.execute("SELECT 1 FROM shipments WHERE shipment_id = ?", (row.payment_ref,)).fetchone():
        conn.execute("UPDATE shipments SET shipped_date = ?, total = ?, card_last4 = ?, payer = ?, charge_code = COALESCE(?, charge_code) WHERE shipment_id = ?",
                     (row.txn_date, row.amount, row.card_last4, row.payer, code, row.payment_ref))
        return "charge"
    # An email-derived shipment for the same order and amount without card info: enrich it.
    twin = conn.execute(
        """SELECT shipment_id FROM shipments WHERE order_id = ? AND source = 'email' AND card_last4 IS NULL
           AND ABS(total - ?) < 0.005 ORDER BY shipped_date LIMIT 1""", (row.order_id, row.amount)).fetchone()
    if twin:
        conn.execute("UPDATE shipments SET card_last4 = ?, payer = ?, payment_ref = ?, shipped_date = ?, charge_code = COALESCE(?, charge_code) WHERE shipment_id = ?",
                     (row.card_last4, row.payer, row.payment_ref, row.txn_date, code, twin["shipment_id"]))
        return "merged"
    conn.execute(
        """INSERT INTO shipments(shipment_id, order_id, shipped_date, total, currency, source, card_last4, payer, payment_ref, charge_code, created_at)
           VALUES (?,?,?,?,?,'report',?,?,?,?,?)""",
        (row.payment_ref, row.order_id, row.txn_date, row.amount, row.currency, row.card_last4, row.payer, row.payment_ref, code, now_iso()))
    return "charge"


def add_invoice_file(conn, order_id, path, label=None):
    conn.execute("INSERT OR IGNORE INTO invoice_files(order_id, path, label, created_at) VALUES (?,?,?,?)",
                 (order_id, path, label, now_iso()))


def invoice_files(conn, order_id):
    return [r["path"] for r in conn.execute("SELECT path FROM invoice_files WHERE order_id = ? ORDER BY label, id", (order_id,))]


def mark_processed(conn, message_id, source, outcome, detail=None):
    conn.execute(
        "INSERT OR REPLACE INTO processed_messages(message_id, source, outcome, detail, processed_at) VALUES (?,?,?,?,?)",
        (message_id, source, outcome, detail, now_iso()),
    )


def is_processed(conn, message_id) -> bool:
    return conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)).fetchone() is not None


# ---------- bank ----------

def insert_bank_transaction(conn, t) -> bool:
    """Returns True if inserted, False if it was a duplicate."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO bank_transactions(txn_date, description, amount, currency, account, source_file, raw_row, fingerprint, is_amazon, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (t.txn_date, t.description, t.amount, t.currency, t.account, t.source_file,
         json.dumps(t.raw_row, ensure_ascii=False), t.fingerprint, int(t.is_amazon), now_iso()),
    )
    return cur.rowcount == 1


# ---------- matches ----------

def save_match(conn, bank_txn_id, kind, ref_ids, order_id, method, confidence, note=None):
    conn.execute("DELETE FROM matches WHERE bank_txn_id = ?", (bank_txn_id,))
    conn.execute(
        "INSERT INTO matches(bank_txn_id, kind, ref_ids, order_id, method, confidence, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (bank_txn_id, kind, json.dumps(list(ref_ids)), order_id, method, confidence, note, now_iso()),
    )


def delete_match(conn, bank_txn_id):
    conn.execute("DELETE FROM matches WHERE bank_txn_id = ?", (bank_txn_id,))


def matched_ref_ids(conn, kind):
    """Set of shipment/order/refund ids already used by a match."""
    used = set()
    for row in conn.execute("SELECT kind, ref_ids FROM matches"):
        k = row["kind"]
        if kind == "shipment" and k in ("shipment", "shipment_group"):
            used.update(json.loads(row["ref_ids"]))
        elif kind == k:
            used.update(json.loads(row["ref_ids"]))
    return used
