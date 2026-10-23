from app.bank import importer, quick


def test_headered_single_amount_card_export():
    csv_bytes = b"Date,Description,Amount\n2026-10-17,AMZN Mktp CA*2A3BC4DEF,106.39\n2026-10-16,STARBUCKS,5.25\n2026-10-09,AMAZON.CA REFUND,-19.99\n"
    rows = importer.read_rows(csv_bytes)
    m = importer.detect_mapping(rows)
    assert (m.date, m.description, m.amount, m.has_header, m.amount_sign) == (0, 1, 2, True, 1)
    txns, errors = importer.apply_mapping(rows, m, account="RBC Visa 3874")
    assert not errors and len(txns) == 3
    assert txns[0].amount == 106.39 and txns[0].is_amazon
    assert txns[1].is_amazon is False
    assert txns[2].amount == -19.99


def test_headered_negative_charges_chequing():
    csv_bytes = b"Posted Date,Payee,Amount,Balance\n10/17/2026,AMAZON.CA*ABC,-106.39,1000.00\n10/16/2026,PAYROLL,2500.00,1106.39\n"
    rows = importer.read_rows(csv_bytes)
    m = importer.detect_mapping(rows)
    assert m.amount_sign == -1
    txns, _ = importer.apply_mapping(rows, m)
    assert txns[0].amount == 106.39 and txns[1].amount == -2500.00


def test_debit_credit_columns():
    csv_bytes = b"Date,Description,Debit,Credit\n2026-10-17,AMZN Mktp CA,106.39,\n2026-10-09,AMAZON.CA,,19.99\n"
    rows = importer.read_rows(csv_bytes)
    m = importer.detect_mapping(rows)
    assert (m.debit, m.credit) == (2, 3)
    txns, _ = importer.apply_mapping(rows, m)
    assert [t.amount for t in txns] == [106.39, -19.99]


def test_headerless_rbc_style():
    csv_bytes = b'10/17/2026,"AMZN Mktp CA*2A3BC4DEF",106.39,,893.61\n10/16/2026,"PAYMENT - THANK YOU",,500.00,1000.00\n10/14/2026,"AMAZON.CA*XYZ",56.16,,1393.61\n'
    rows = importer.read_rows(csv_bytes)
    m = importer.detect_mapping(rows)
    assert m.has_header is False and m.date == 0 and m.description == 1
    assert (m.debit, m.credit) == (2, 3)
    txns, _ = importer.apply_mapping(rows, m)
    assert [t.amount for t in txns] == [106.39, -500.0, 56.16]


def test_quick_paste():
    text = "Oct 17, 2026   52.63\nOct 14, 2026  AMZN Mktp CA  $105.26\n2026-10-13\n10/11/2026 19.99 CR\n\nSept 2, 2026 1,234.56\nnot a line\n"
    txns, errors = quick.parse_quick_lines(text, account="RBC Visa 3874")
    assert len(errors) == 1 and "not a line" in errors[0]
    assert [(t.txn_date, t.amount) for t in txns] == [("2026-10-17", 52.63), ("2026-10-14", 105.26), ("2026-10-13", None), ("2026-10-11", -19.99), ("2026-09-02", 1234.56)]
    assert txns[1].description == "AMZN Mktp CA"
    assert all(t.is_amazon for t in txns)


def test_transactions_page_paste():
    from app.sources.amazon_transactions import parse_transactions_text
    text = """Your Transactions
Completed
October 17, 2026
Visa ****3874
-$106.39
Order #701-5120229-1371409
Amazon.ca
October 14, 2026
Mastercard ****1012
-CA$56.16
Order #701-8862615-1913846
October 9, 2026
Visa ****3874
+$19.99
Refund: Order #701-0000004-0000004
"""
    rows, notes = parse_transactions_text(text)
    assert [(r.txn_date, r.card_last4, r.amount, r.is_refund, r.order_id) for r in rows] == [
        ("2026-10-17", "3874", 106.39, False, "701-5120229-1371409"),
        ("2026-10-14", "1012", 56.16, False, "701-8862615-1913846"),
        ("2026-10-09", "3874", 19.99, True, "701-0000004-0000004"),
    ]
    assert not notes
