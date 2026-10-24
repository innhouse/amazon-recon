from pathlib import Path

from app import db
from app.bank import quick
from app.bank.importer import BankTxn
from app.recon import matcher
from app.sources import amazon_report, eml_source

FIX = Path(__file__).parent / "fixtures"


def txn(conn, d, amount, account="RBC Visa 3874", desc="AMZN Mktp CA"):
    import hashlib
    t = BankTxn(txn_date=d, description=desc, amount=amount, currency="CAD", account=account, source_file="t", raw_row=[],
                fingerprint=hashlib.sha1(f"{d}{amount}{desc}{account}".encode()).hexdigest(), is_amazon=True)
    assert db.insert_bank_transaction(conn, t)
    return conn.execute("SELECT id FROM bank_transactions WHERE fingerprint = ?", (t.fingerprint,)).fetchone()[0]


def load_emails(conn):
    for name in ("ordered.eml", "shipped.eml", "shipped_html.eml"):
        assert eml_source.import_bytes(conn, (FIX / name).read_bytes(), name) in ("order", "shipment")


def test_email_shipments_match_split_order(conn):
    load_emails(conn)
    assert conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 2
    a = txn(conn, "2026-10-08", 52.63)
    b = txn(conn, "2026-10-10", 52.63)
    s = matcher.run_auto_match(conn)
    assert s["matched"] == 2
    m = {r["bank_txn_id"]: r for r in conn.execute("SELECT * FROM matches")}
    assert m[a]["method"] == "exact_shipment" and m[a]["ref_ids"] == '["Nx0QzhDs1"]'
    assert m[b]["ref_ids"] == '["Ab9ZzYx2"]'


def test_whole_order_fallback_and_group(conn):
    load_emails(conn)
    # One statement line for both boxes combined -> shipment_group (2 x 52.63 = 105.26 = order total)
    t = txn(conn, "2026-10-09", 105.26)
    s = matcher.run_auto_match(conn)
    assert s["matched"] == 1
    row = conn.execute("SELECT * FROM matches").fetchone()
    assert row["method"] in ("shipment_group", "exact_order")


def test_report_import_and_card_filter(conn):
    stats, errors = amazon_report.import_report(conn, (FIX / "report.csv").read_bytes())
    assert not errors and stats["charge"] == 6 and stats["refund"] == 1
    # Same amount on two cards the same day: the card in the account label decides.
    a = txn(conn, "2026-10-14", 56.16, account="RBC Visa 3874")
    b = txn(conn, "2026-10-14", 56.16, account="RBC Visa 1012")
    r = txn(conn, "2026-10-10", -19.99, account="RBC Visa 3874", desc="AMAZON.CA REFUND")
    s = matcher.run_auto_match(conn)
    assert s["matched"] == 3, s
    m = {row["bank_txn_id"]: row for row in conn.execute("SELECT * FROM matches")}
    assert m[a]["order_id"] == "701-0000002-0000002"
    assert m[b]["order_id"] == "701-0000003-0000003"
    assert m[r]["kind"] == "refund"


def test_report_merges_with_email_shipment(conn):
    load_emails(conn)
    amazon_report.import_report(conn, (FIX / "report.csv").read_bytes())
    rows = conn.execute("SELECT * FROM shipments WHERE order_id = '702-1234567-8901234' ORDER BY shipped_date").fetchall()
    assert len(rows) == 2                      # not 3: the report line merged into the email shipment
    assert any(r["card_last4"] == "3874" and r["source"] == "email" for r in rows)


def test_date_only_request_uses_card_and_date(conn):
    amazon_report.import_report(conn, (FIX / "report.csv").read_bytes())
    txns, _ = quick.parse_quick_lines("Oct 17, 2026\nOct 14, 2026", account="RBC Visa 3874")
    for t in txns:
        db.insert_bank_transaction(conn, t)
    s = matcher.run_auto_match(conn)
    # Oct 17 has exactly one 3874 charge -> auto; Oct 14 has charges from two different orders -> review
    assert s["matched"] == 1 and s["ambiguous"] == 1
    queue = matcher.review_queue(conn)
    assert len(queue) == 1 and {c.ref_ids[0] for c in queue[0][1]} == {"REF002", "REF003", "REF007"}
    assert all(c.ref_ids[0] != "REF004" for c in queue[0][1])   # the 1012 charge is excluded by card


def test_unmatch_frees_shipment(conn):
    load_emails(conn)
    a = txn(conn, "2026-10-08", 52.63)
    matcher.run_auto_match(conn)
    db.delete_match(conn, a)
    assert matcher.run_auto_match(conn)["matched"] == 1


def test_shipment_level_report_and_statement_code(conn):
    from app.sources import amazon_report
    stats, errors = amazon_report.import_shipment_report(conn, (FIX / "shipment_report.csv").read_bytes())
    assert not errors and stats["charge"] == 3          # the pending shipment is skipped
    row = conn.execute("SELECT charge_code, total FROM shipments WHERE shipment_id = 'REF003'").fetchone()
    assert row["charge_code"] == "5E67Y2LA2" and row["total"] == 42.12
    item = conn.execute("SELECT description, quantity FROM order_items WHERE shipment_id = 'REF003'").fetchone()
    assert item["description"] == "Shower rod flange set" and item["quantity"] == 6
    # A statement line with the code matches exactly, even with the wrong date and no card.
    t = txn(conn, "2026-10-20", 42.12, account="RBC Visa", desc="AMAZON* 5E67Y2LA2 VANCOUVER BC")
    assert matcher.run_auto_match(conn)["matched"] == 1
    m = conn.execute("SELECT * FROM matches").fetchone()
    assert m["method"] == "statement_code" and m["ref_ids"] == '["REF003"]'
