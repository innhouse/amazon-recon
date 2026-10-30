import io
import zipfile
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def test_full_flow(client):
    assert client.get("/").status_code == 200
    r = client.post("/import/amazon-report", files={"file": ("report.csv", (FIX / "report.csv").read_bytes(), "text/csv")}, data={"marketplace": "amazon.ca"})
    assert r.status_code == 200 and "Reconciliation report imported" in r.text
    r = client.post("/import/eml", files=[("files", ("shipped.eml", (FIX / "shipped.eml").read_bytes(), "message/rfc822")),
                                          ("files", ("ordered.eml", (FIX / "ordered.eml").read_bytes(), "message/rfc822"))])
    assert "Imported .eml" in r.text
    r = client.post("/bank/quick", data={"text": "Oct 17, 2026 106.39\nOct 14, 2026 42.12\nOct 7, 2026 52.63", "account": "RBC Visa 3874", "currency": "CAD"})
    assert "Added 3 charges" in r.text and "3 matched" in r.text
    assert client.get("/orders").status_code == 200
    assert client.get("/charges?card=3874").status_code == 200
    assert client.get("/bank").status_code == 200
    assert "702-1234567-8901234" in client.get("/charges.csv?card=3874").text
    csv_text = client.get("/export/csv?account=RBC Visa 3874").text
    assert csv_text.count("matched") == 3
    r = client.get("/export/package?account=RBC Visa 3874")
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "reconciliation.csv" in names and sum(n.endswith(".pdf") for n in names) == 3
    assert client.get("/invoices/702-1234567-8901234.pdf").headers["content-type"] == "application/pdf"


def test_csv_import_preview_and_confirm(client):
    csv_bytes = b"Date,Description,Amount\n2026-10-17,AMZN Mktp CA*2A3BC4DEF,106.39\n2026-10-16,COFFEE,5.25\n"
    r = client.post("/bank/import/preview", files={"file": ("visa.csv", csv_bytes, "text/csv")}, data={"account": "RBC Visa 3874", "currency": "CAD"})
    assert r.status_code == 200 and "1 look like Amazon" in r.text
    token = r.text.split('name="token" value="')[1].split('"')[0]
    r = client.post("/bank/import/confirm", data={"token": token, "account": "RBC Visa 3874", "currency": "CAD", "filename": "visa.csv",
                                                  "date_col": 0, "desc_col": 1, "amount_col": 2, "debit_col": "", "credit_col": "", "amount_sign": 1, "has_header": "1"})
    assert "Imported 2 new transactions" in r.text


def test_transactions_paste_route(client):
    text = "October 17, 2026\nVisa ****3874\n-$106.39\nOrder #701-5120229-1371409\n"
    r = client.post("/import/amazon-transactions", data={"text": text, "marketplace": "amazon.ca", "card_hint": ""})
    assert "Transactions page imported" in r.text
    assert "701-5120229-1371409" in client.get("/charges?card=3874").text


def test_orders_search(client):
    r = client.post("/import/amazon-report", files={"file": ("report.csv", (FIX / "report.csv").read_bytes(), "text/csv")}, data={"marketplace": "amazon.ca"})
    r = client.get("/orders?q=701-0000001-0000001")
    assert r.status_code == 200 and "701-0000001-0000001" in r.text and "701-0000002-0000002" not in r.text


def test_orders_report_import_adds_items(client):
    client.post("/import/amazon-report", files={"file": ("report.csv", (FIX / "report.csv").read_bytes(), "text/csv")}, data={"marketplace": "amazon.ca"})
    orders_csv = (
        "Order Date,Order ID,Account Group,Currency,Order net total,Account User,Payment Reference ID,Payment Amount,Payment Identifier,Title,Purchase PPU,Item Quantity\n"
        '2026/10/15,701-0000001-0000001,"Example Ltd",CAD,"106.39",Pat Example,REF001,"106.39",="3874","Vanity light 48 inch","94.99",1\n'
        '2026/10/12,701-0000002-0000002,"Example Ltd",CAD,"154.44",Pat Example,REF002,"56.16",="3874","Shower rod flange set","6.27",22\n'
        '2026/10/12,701-0000002-0000002,"Example Ltd",CAD,"154.44",Pat Example,REF003,"42.12",="3874","Shower rod flange set","6.27",22\n'
    ).encode()
    r = client.post("/import/amazon-report", files={"file": ("orders.csv", orders_csv, "text/csv")}, data={"marketplace": "amazon.ca"})
    assert "Orders report imported" in r.text and "items: 2" in r.text
    page = client.get("/charges?card=3874").text
    assert "Vanity light" in page and "22x Shower rod" in page


def test_cloud_sync_payload(tmp_path, monkeypatch):
    from app import config, db
    from app.cloud import sync
    from app.sources import amazon_report
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    conn = db.connect(tmp_path / "s.sqlite3")
    amazon_report.import_shipment_report(conn, (FIX / "shipment_report.csv").read_bytes())
    pdf = tmp_path / "invoices" / "amazon-invoice-701-0000002-0000002-01.pdf"
    pdf.parent.mkdir(parents=True); pdf.write_bytes(b"%PDF-1.4 test")
    db.add_invoice_file(conn, "701-0000002-0000002", str(pdf), "01")
    payload = sync.build_payload(conn)
    assert payload["summary"]["charges"] == 3 and payload["summary"]["cards"] == ["3874"]
    c = payload["charges"]["REF003"]
    assert c["code"] == "5E67Y2LA2" and c["amount"] == 42.12 and c["items"][0]["quantity"] == 6
    assert c["invoice_files"] == ["invoices/701-0000002-0000002/amazon-invoice-701-0000002-0000002-01.pdf"]
    assert sync.upload(conn, dry_run=True, log=lambda *a: None)["pdfs_total"] == 1


def test_monthly_packages(tmp_path, monkeypatch):
    import io, zipfile
    from app import config, db, monthly
    from app.sources import amazon_report
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    conn = db.connect(tmp_path / "m.sqlite3")
    amazon_report.import_shipment_report(conn, (FIX / "shipment_report.csv").read_bytes())
    pdf = tmp_path / "invoices" / "amazon-invoice-701-0000002-0000002-01.pdf"
    pdf.parent.mkdir(parents=True); pdf.write_bytes(b"%PDF-1.4 test")
    db.add_invoice_file(conn, "701-0000002-0000002", str(pdf), "01")
    assert monthly.previous_month(__import__("datetime").date(2026, 1, 15)) == "2025-12"
    pk = monthly.build_packages(conn, "2026-10")
    assert [p.card for p in pk] == ["3874"]
    p = pk[0]
    assert len(p.charges) == 3 and p.pdf_count == 1 and p.missing_orders == ["701-0000001-0000001"]
    assert round(p.total, 2) == 204.67
    names = zipfile.ZipFile(io.BytesIO(p.zip_bytes)).namelist()
    assert "701-0000002-0000002/amazon-invoice-701-0000002-0000002-01.pdf" in names and "statement-lines-card3874-2026-10.csv" in names
    body = monthly.email_body(p)
    assert "card #3874" in body and "AMAZON* 5E67Y2LA2" in body and "not yet available" in body
    monkeypatch.setenv("RECON_MAIL_TO", "wendy@example.com")
    out = monthly.run(conn, month="2026-10", mode="draft", out_dir=tmp_path / "out", log=lambda *a: None)
    assert (tmp_path / "out" / "email-card3874-2026-10.eml").exists() and (tmp_path / "out" / out[0].filename).exists()
