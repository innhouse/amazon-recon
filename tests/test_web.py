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
