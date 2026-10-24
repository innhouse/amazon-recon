"""Web UI. Run with:  uvicorn app.main:app --reload   then open http://127.0.0.1:8000"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, export
from .bank import importer, quick
from .invoices import fetcher
from .recon import matcher
from .sources import amazon_orders_report, amazon_report, amazon_transactions, eml_source, gmail_source, imap_source

BASE = Path(__file__).parent
app = FastAPI(title="Amazon invoice reconciliation")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["money"] = lambda v: "" if v is None else f"{v:,.2f}"
templates.env.filters["fromjson"] = json.loads


def render(request: Request, name: str, **ctx):
    flash = request.cookies.get("flash")
    resp = templates.TemplateResponse(request, name, {"flash": flash, **ctx})
    if flash:
        resp.delete_cookie("flash")
    return resp


def redirect(url: str, flash: str | None = None):
    resp = RedirectResponse(url, status_code=303)
    if flash:
        resp.set_cookie("flash", flash[:900], max_age=30)
    return resp


# ---------- dashboard ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db.session() as conn:
        orders = [r[0] for r in conn.execute("SELECT order_id FROM orders")]
        with_pdf = sum(1 for o in orders if any(Path(f).exists() for f in db.invoice_files(conn, o)))
        pdfs = sum(1 for r in conn.execute("SELECT path FROM invoice_files") if Path(r[0]).exists())
        stats = {"orders": len(orders), "orders_with_pdf": with_pdf, "pdfs": pdfs,
                 "charges": conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]}
        cards = [r[0] for r in conn.execute("SELECT DISTINCT card_last4 FROM shipments WHERE card_last4 IS NOT NULL ORDER BY 1")]
        last_import = db.kv_get(conn, "last_report_import")
        last_fetch = db.kv_get(conn, "last_invoice_fetch")
    return render(request, "home.html", stats=stats, cards=cards, last_import=last_import, last_fetch=last_fetch,
                  playwright_ok=fetcher.playwright_available())


@app.get("/advanced", response_class=HTMLResponse)
def dashboard(request: Request):
    with db.session() as conn:
        counts = {
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "shipments": conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
            "refunds": conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0],
            "bank": conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE is_amazon = 1").fetchone()[0],
            "matched": conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
            "unmatched": conn.execute("SELECT COUNT(*) FROM bank_transactions t LEFT JOIN matches m ON m.bank_txn_id = t.id WHERE t.is_amazon = 1 AND m.id IS NULL").fetchone()[0],
            "pdfs": conn.execute("SELECT COUNT(*) FROM orders WHERE invoice_pdf IS NOT NULL").fetchone()[0],
        }
        accounts = [r[0] for r in conn.execute("SELECT DISTINCT account FROM bank_transactions WHERE account IS NOT NULL ORDER BY 1")]
        last_imap = db.kv_get(conn, "imap_last_sync_date")
        last_gmail = db.kv_get(conn, "gmail_last_sync_date")
        unbilled = matcher.unbilled_shipments(conn)[:10]
    imap = imap_source.settings_from_env()
    return render(request, "dashboard.html", counts=counts, accounts=accounts, last_imap=last_imap, last_gmail=last_gmail,
                  imap_user=imap["user"], imap_host=imap["host"], gmail_ok=gmail_source.is_configured(),
                  playwright_ok=fetcher.playwright_available(), unbilled=unbilled)


# ---------- email sources ----------

@app.post("/sync/imap")
def sync_imap(host: str = Form(None), port: int = Form(993), user: str = Form(None), password: str = Form(None),
              folder: str = Form("INBOX"), lookback: int = Form(400)):
    env = imap_source.settings_from_env()
    host, user, password, folder = host or env["host"], user or env["user"], password or env["password"], folder or env["folder"]
    if not (host and user and password):
        return redirect("/", "IMAP: host, user and app-specific password are required (form fields or IMAP_* environment variables).")
    try:
        with db.session() as conn:
            stats = imap_source.sync(conn, host=host, port=port, user=user, password=password, folder=folder, lookback_days=lookback)
    except Exception as exc:
        return redirect("/", f"IMAP sync failed: {exc}")
    return redirect("/orders", f"IMAP sync done. {stats.summary()}")


@app.post("/sync/gmail")
def sync_gmail():
    try:
        with db.session() as conn:
            stats = gmail_source.sync(conn)
    except Exception as exc:
        return redirect("/", f"Gmail sync failed: {exc}")
    return redirect("/orders", f"Gmail sync done. {stats.summary()}")


@app.post("/import/eml")
async def import_eml(files: list[UploadFile] = File(...)):
    stats = eml_source.SyncStats()
    with db.session() as conn:
        for f in files:
            eml_source.import_bytes(conn, await f.read(), f.filename or "upload.eml", stats)
    return redirect("/orders", f"Imported .eml files. {stats.summary()}")


@app.post("/import/amazon-report")
async def import_amazon_report(file: UploadFile = File(None), marketplace: str = Form("amazon.ca")):
    content = await file.read() if file else b""
    if not content:
        return redirect("/", "No file chosen. Click 'Choose File' next to CSV, pick the reconciliation report, then Import report.")
    import csv as _csv, io as _io
    header = next(_csv.reader(_io.StringIO(content.decode("utf-8-sig", errors="replace"))), [])
    try:
        with db.session() as conn:
            if amazon_report.looks_like_shipment_report(header):
                stats, errors = amazon_report.import_shipment_report(conn, content, marketplace=marketplace)
                kind = "Shipment-level reconciliation report"
                db.kv_set(conn, "last_report_import", date.today().isoformat())
            elif amazon_orders_report.looks_like_orders_report(header):
                stats, errors = amazon_orders_report.import_orders_report(conn, content, marketplace=marketplace)
                kind = "Orders report"
            else:
                stats, errors = amazon_report.import_report(conn, content, marketplace=marketplace)
                kind = "Reconciliation report"
                db.kv_set(conn, "last_report_import", date.today().isoformat())
            summary = matcher.run_auto_match(conn)
    except ValueError as exc:
        return redirect("/", str(exc))
    msg = f"{kind} imported: {stats.summary()}."
    if errors:
        msg += f" {len(errors)} rows skipped: " + "; ".join(errors[:3])
    return redirect("/", msg)


@app.post("/import/amazon-transactions")
def import_amazon_transactions(text: str = Form(""), marketplace: str = Form("amazon.ca"), card_hint: str = Form("")):
    if not text.strip():
        return redirect("/", "The Transactions box was empty. To import the Business report, use the 'CSV' file box at the top of the dashboard.")
    with db.session() as conn:
        stats, errors = amazon_transactions.import_text(conn, text, marketplace=marketplace, card_hint=card_hint or None)
        summary = matcher.run_auto_match(conn)
    if not stats:
        return redirect("/", "No transactions recognised in the pasted text. Copy the whole 'Your Transactions' page (Ctrl/Cmd+A, Ctrl/Cmd+C) and paste it.")
    msg = f"Transactions page imported ({stats.summary()}). Auto-match: {summary['matched']} matched, {summary['ambiguous']} need review."
    if errors:
        msg += " Notes: " + "; ".join(errors[:3])
    return redirect("/charges", msg)


@app.get("/charges", response_class=HTMLResponse)
def charges(request: Request, card: str = "", date_from: str = "", date_to: str = ""):
    """Amazon's own list of card charges (from the reconciliation report and shipment emails)."""
    with db.session() as conn:
        where, args = ["1=1"], []
        if card:
            where.append("s.card_last4 = ?"); args.append(card)
        if date_from:
            where.append("s.shipped_date >= ?"); args.append(date_from)
        if date_to:
            where.append("s.shipped_date <= ?"); args.append(date_to)
        rows = conn.execute(f"""SELECT s.*, o.order_date, o.invoice_url, o.invoice_pdf, o.marketplace,
                                       (SELECT COUNT(*) FROM matches m WHERE m.ref_ids LIKE '%' || s.shipment_id || '%') AS matched
                                FROM shipments s JOIN orders o USING(order_id) WHERE {' AND '.join(where)}
                                ORDER BY s.shipped_date DESC, s.order_id""", args).fetchall()
        cards = [r[0] for r in conn.execute("SELECT DISTINCT card_last4 FROM shipments WHERE card_last4 IS NOT NULL ORDER BY 1")]
        total = sum(r["total"] or 0 for r in rows)
        order_ids = {r["order_id"] for r in rows}
        pdf_counts = {oid: sum(1 for f in db.invoice_files(conn, oid) if Path(f).exists()) for oid in order_ids}
        items = {}
        for r in rows:
            # Prefer the item shipped on this exact charge; fall back to the order's items.
            its = conn.execute("SELECT description, quantity FROM order_items WHERE shipment_id = ? ORDER BY id", (r["shipment_id"],)).fetchall()
            if not its:
                its = conn.execute("SELECT description, quantity FROM order_items WHERE order_id = ? AND shipment_id IS NULL ORDER BY id", (r["order_id"],)).fetchall()
            if its:
                first = f"{its[0]['quantity']}x {its[0]['description'][:60]}"
                items[r["shipment_id"]] = first + (f" (+{len(its) - 1} more)" if len(its) > 1 else "")
    return render(request, "charges.html", rows=rows, cards=cards, card=card, date_from=date_from, date_to=date_to, total=total,
                  pdf_counts=pdf_counts, items=items, pdf_total=sum(pdf_counts.values()), orders_without=sum(1 for v in pdf_counts.values() if not v),
                  playwright_ok=fetcher.playwright_available())


@app.get("/charges.csv")
def charges_csv(card: str = "", date_from: str = "", date_to: str = ""):
    import csv, io
    with db.session() as conn:
        where, args = ["1=1"], []
        if card:
            where.append("s.card_last4 = ?"); args.append(card)
        if date_from:
            where.append("s.shipped_date >= ?"); args.append(date_from)
        if date_to:
            where.append("s.shipped_date <= ?"); args.append(date_to)
        rows = conn.execute(f"""SELECT s.shipped_date, s.card_last4, s.charge_code, s.total, s.currency, s.order_id, o.order_date, s.payer, s.payment_ref, o.invoice_url
                                FROM shipments s JOIN orders o USING(order_id) WHERE {' AND '.join(where)} ORDER BY s.shipped_date, s.order_id""", args).fetchall()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["charge_date", "card", "statement_code", "amount", "currency", "order_id", "order_date", "account_user", "payment_ref", "invoice_url"])
    for r in rows:
        w.writerow(list(r))
    name = f"amazon-charges-{card or 'all'}-{date_from or 'start'}-{date_to or 'end'}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------- orders ----------

@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request, q: str = ""):
    with db.session() as conn:
        where, args = "", []
        if q:
            where = "WHERE o.order_id LIKE ? OR EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND i.description LIKE ?)"
            args = [f"%{q}%", f"%{q}%"]
        rows = conn.execute(f"SELECT o.* FROM orders o {where} ORDER BY o.order_date DESC, o.order_id", args).fetchall()
        used = db.matched_ref_ids(conn, "shipment") | db.matched_ref_ids(conn, "order")
        data = []
        for o in rows:
            ships = conn.execute("SELECT * FROM shipments WHERE order_id = ? ORDER BY shipped_date", (o["order_id"],)).fetchall()
            items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (o["order_id"],)).fetchall()
            match_rows = conn.execute("SELECT m.*, t.txn_date, t.amount, t.account FROM matches m JOIN bank_transactions t ON t.id = m.bank_txn_id WHERE m.order_id = ?", (o["order_id"],)).fetchall()
            data.append({"o": o, "ships": ships, "lines": items, "matches": match_rows,
                         "used": o["order_id"] in used or any(s["shipment_id"] in used for s in ships)})
    return render(request, "orders.html", data=data, q=q)


# ---------- bank ----------

@app.get("/bank", response_class=HTMLResponse)
def bank(request: Request, account: str = "", show: str = "amazon"):
    with db.session() as conn:
        where, args = ["1=1"], []
        if show == "amazon":
            where.append("t.is_amazon = 1")
        if account:
            where.append("t.account = ?"); args.append(account)
        rows = conn.execute(f"""SELECT t.*, m.kind, m.ref_ids, m.order_id, m.method, m.confidence
                                FROM bank_transactions t LEFT JOIN matches m ON m.bank_txn_id = t.id
                                WHERE {' AND '.join(where)} ORDER BY t.txn_date DESC, t.id DESC""", args).fetchall()
        accounts = [r[0] for r in conn.execute("SELECT DISTINCT account FROM bank_transactions WHERE account IS NOT NULL ORDER BY 1")]
    return render(request, "bank.html", rows=rows, accounts=accounts, account=account, show=show)


@app.get("/bank/import", response_class=HTMLResponse)
def bank_import_form(request: Request):
    return render(request, "bank_import.html", preview=None)


@app.post("/bank/import/preview", response_class=HTMLResponse)
async def bank_import_preview(request: Request, file: UploadFile = File(...), account: str = Form(""), currency: str = Form("CAD")):
    content = await file.read()
    rows = importer.read_rows(content)
    header = rows[0] if rows else []
    if amazon_report.looks_like_shipment_report(header) or amazon_report.looks_like_report(header) or amazon_orders_report.looks_like_orders_report(header):
        # An Amazon report was dropped into the bank-statement box: import it as a report instead.
        with db.session() as conn:
            if amazon_report.looks_like_shipment_report(header):
                stats, errors = amazon_report.import_shipment_report(conn, content)
            elif amazon_orders_report.looks_like_orders_report(header):
                stats, errors = amazon_orders_report.import_orders_report(conn, content)
            else:
                stats, errors = amazon_report.import_report(conn, content)
            db.kv_set(conn, "last_report_import", date.today().isoformat())
        return redirect("/", f"That file is an Amazon report, so it was imported as one: {stats.summary()}. Bank statements go in this box; Amazon reports go in Step 1 on the Home page.")
    try:
        m = importer.detect_mapping(rows)
        error = None
    except ValueError as exc:
        m = importer.Mapping(date=0, description=1, amount=2, has_header=importer._looks_like_header(rows[0]) if rows else True)
        error = str(exc)
    txns, errors = importer.apply_mapping(rows, m, source_file=file.filename, account=account or None, currency=currency)
    tmp = config.DATA_DIR / "uploads"; tmp.mkdir(exist_ok=True)
    token = os.urandom(6).hex()
    (tmp / f"{token}.csv").write_bytes(content)
    ncols = max(len(r) for r in rows) if rows else 0
    return render(request, "bank_import.html", preview={"rows": rows[:8], "ncols": ncols, "mapping": m, "txns": txns[:12],
                  "count": len(txns), "amazon": sum(t.is_amazon for t in txns), "errors": errors[:10], "token": token,
                  "filename": file.filename, "account": account, "currency": currency, "error": error})


@app.post("/bank/import/confirm")
def bank_import_confirm(token: str = Form(...), account: str = Form(""), currency: str = Form("CAD"), filename: str = Form(""),
                        date_col: int = Form(...), desc_col: int = Form(...), amount_col: str = Form(""), debit_col: str = Form(""),
                        credit_col: str = Form(""), amount_sign: int = Form(1), has_header: str = Form("1")):
    path = config.DATA_DIR / "uploads" / f"{token}.csv"
    if not path.exists():
        return redirect("/bank/import", "Upload expired, please upload the file again.")
    rows = importer.read_rows(path.read_bytes())
    m = importer.Mapping(date=date_col, description=desc_col, amount=int(amount_col) if amount_col != "" else None,
                         debit=int(debit_col) if debit_col != "" else None, credit=int(credit_col) if credit_col != "" else None,
                         amount_sign=amount_sign, has_header=has_header == "1")
    txns, errors = importer.apply_mapping(rows, m, source_file=filename, account=account or None, currency=currency)
    inserted = 0
    with db.session() as conn:
        for t in txns:
            inserted += db.insert_bank_transaction(conn, t)
        summary = matcher.run_auto_match(conn)
    path.unlink(missing_ok=True)
    return redirect("/review", f"Imported {inserted} new transactions ({len(txns) - inserted} duplicates skipped, {len(errors)} rows with errors). "
                               f"Auto-match: {summary['matched']} matched, {summary['ambiguous']} need review, {summary['unmatched']} without candidates.")


@app.get("/bank/quick", response_class=HTMLResponse)
def quick_form(request: Request):
    return render(request, "quick.html")


@app.post("/bank/quick")
def quick_submit(text: str = Form(""), account: str = Form(""), currency: str = Form("CAD")):
    if not text.strip() or not account.strip():
        return redirect("/bank/quick", "Both the account label and at least one charge line are required.")
    txns, errors = quick.parse_quick_lines(text, account=account.strip(), currency=currency)
    inserted = 0
    with db.session() as conn:
        for t in txns:
            inserted += db.insert_bank_transaction(conn, t)
        summary = matcher.run_auto_match(conn)
    msg = f"Added {inserted} charges for {account}. Auto-match: {summary['matched']} matched, {summary['ambiguous']} need review, {summary['unmatched']} without candidates."
    if errors:
        msg += " Problems: " + "; ".join(errors[:5])
    return redirect(f"/review?account={account.strip()}", msg)


@app.post("/bank/{txn_id}/delete")
def bank_delete(txn_id: int):
    with db.session() as conn:
        db.delete_match(conn, txn_id)
        conn.execute("DELETE FROM bank_transactions WHERE id = ?", (txn_id,))
    return redirect("/bank", "Transaction deleted.")


@app.post("/bank/{txn_id}/flag")
def bank_flag(txn_id: int, is_amazon: int = Form(...)):
    with db.session() as conn:
        conn.execute("UPDATE bank_transactions SET is_amazon = ? WHERE id = ?", (is_amazon, txn_id))
    return redirect("/bank?show=all", "Updated.")


# ---------- matching ----------

@app.post("/match/run")
def match_run(include_all: int = Form(0)):
    with db.session() as conn:
        s = matcher.run_auto_match(conn, include_non_amazon=bool(include_all))
    return redirect("/review", f"Auto-match: {s['matched']} matched, {s['ambiguous']} need review, {s['unmatched']} without candidates.")


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, account: str = ""):
    with db.session() as conn:
        queue = matcher.review_queue(conn)
        if account:
            queue = [(t, c) for t, c in queue if t["account"] == account]
        orders_list = conn.execute("SELECT order_id, order_date, total, currency FROM orders ORDER BY order_date DESC").fetchall()
        shipments_list = conn.execute("SELECT shipment_id, order_id, shipped_date, total, currency FROM shipments ORDER BY shipped_date DESC").fetchall()
        accounts = [r[0] for r in conn.execute("SELECT DISTINCT account FROM bank_transactions WHERE account IS NOT NULL ORDER BY 1")]
    return render(request, "review.html", queue=queue, orders=orders_list, shipments=shipments_list, accounts=accounts, account=account)


@app.post("/match/accept")
def match_accept(txn_id: int = Form(...), kind: str = Form(...), ref_ids: str = Form(...), order_id: str = Form(...),
                 method: str = Form("manual"), confidence: float = Form(1.0), back: str = Form("/review")):
    with db.session() as conn:
        db.save_match(conn, txn_id, kind, json.loads(ref_ids) if ref_ids.startswith("[") else [ref_ids], order_id, method, confidence,
                      "accepted by user" if method != "manual" else "manual match")
    return redirect(back, f"Matched transaction {txn_id} to order {order_id}.")


@app.post("/match/manual")
def match_manual(txn_id: int = Form(...), target: str = Form(...), back: str = Form("/review")):
    """target is 'shipment:<id>' or 'order:<id>'."""
    kind, _, ref = target.partition(":")
    with db.session() as conn:
        if kind == "shipment":
            row = conn.execute("SELECT order_id FROM shipments WHERE shipment_id = ?", (ref,)).fetchone()
            order_id = row["order_id"] if row else None
        else:
            order_id = ref
        if not order_id:
            return redirect(back, "Unknown shipment / order.")
        db.save_match(conn, txn_id, kind, [ref], order_id, "manual", 1.0, "manual match")
    return redirect(back, f"Matched transaction {txn_id} to order {order_id}.")


@app.post("/match/{txn_id}/unmatch")
def unmatch(txn_id: int, back: str = Form("/bank")):
    with db.session() as conn:
        db.delete_match(conn, txn_id)
    return redirect(back, "Match removed.")


# ---------- invoices ----------

@app.post("/invoices/download")
def invoices_download(scope: str = Form("matched"), account: str = Form(""), headless: int = Form(1)):
    if not fetcher.playwright_available():
        return redirect("/", "Playwright is not installed. Run: pip install playwright && playwright install chromium")
    with db.session() as conn:
        if scope == "all":
            ids = [r[0] for r in conn.execute("SELECT order_id FROM orders WHERE invoice_pdf IS NULL")]
        else:
            q = "SELECT DISTINCT m.order_id FROM matches m JOIN orders o ON o.order_id = m.order_id JOIN bank_transactions t ON t.id = m.bank_txn_id WHERE o.invoice_pdf IS NULL"
            args = []
            if account:
                q += " AND t.account = ?"; args.append(account)
            ids = [r[0] for r in conn.execute(q, args)]
        results = fetcher.download_many(conn, ids, headless=bool(headless))
    ok = sum(1 for v in results.values() if v == "ok")
    bad = {k: v for k, v in results.items() if v != "ok"}
    msg = f"Downloaded {ok} official invoice PDFs."
    if bad:
        msg += " Problems: " + "; ".join(f"{k}: {v}" for k, v in list(bad.items())[:5])
        if "signed-out" in bad.values():
            msg += " Amazon session expired: run `python -m app.cli amazon-login` and try again."
    return redirect("/orders", msg)


# The browser automation (Playwright) cannot run inside the web server's own
# process on Windows, so these two routes start it as a separate process using
# the same command-line tool a terminal user would run.

def _cli(*args, wait: bool, timeout: int = 3600):
    import subprocess, sys
    cmd = [sys.executable, "-m", "app.cli", *args]
    if not wait:
        subprocess.Popen(cmd, cwd=str(config.BASE_DIR))
        return None
    return subprocess.run(cmd, cwd=str(config.BASE_DIR), capture_output=True, text=True, timeout=timeout)


@app.post("/amazon/login")
def amazon_login(marketplace: str = Form("amazon.ca"), back: str = Form("/")):
    """Open a visible browser on this computer for the one-time Amazon sign-in."""
    if not fetcher.playwright_available():
        return redirect(back, "Playwright is not installed. In PowerShell: pip install \"playwright==1.49.1\" then python -m playwright install chromium")
    _cli("amazon-login", "--marketplace", marketplace, wait=False)
    return redirect(back, "A browser window is opening on this computer. Sign in to the Amazon account that placed the orders, wait for Your Orders, then close that window.")


@app.post("/invoices/fetch")
def invoices_fetch(card: str = Form(""), date_from: str = Form(""), date_to: str = Form(""), visible: int = Form(0), back: str = Form("")):
    """Fetch every invoice PDF for the charges matching the filter (runs the browser on this computer)."""
    q = f"card={card}&date_from={date_from}&date_to={date_to}"
    target = back or f"/charges?{q}"
    if not fetcher.playwright_available():
        return redirect(target, "Playwright is not installed. In PowerShell: pip install \"playwright==1.49.1\" then python -m playwright install chromium")
    with db.session() as conn:
        ids = fetcher.orders_for_filter(conn, card=card or None, date_from=date_from or None, date_to=date_to or None)
        todo = [o for o in ids if not any(Path(f).exists() for f in db.invoice_files(conn, o))]
    if not todo:
        return redirect(target, f"All {len(ids)} orders already have their invoices.")
    args = ["download-invoices"]
    for o in todo:
        args += ["--order", o]
    if visible:
        args.append("--visible")
    try:
        proc = _cli(*args, wait=True)
    except Exception as exc:
        return redirect(target, f"Could not start the fetch: {exc}")
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    results = {}
    for l in lines:
        parts = l.split(" ", 1)
        if len(parts) == 2 and parts[0].count("-") == 2 and parts[0][:3].isdigit():
            results[parts[0]] = parts[1]
    ok = sum(1 for v in results.values() if v.startswith("ok"))
    bad = {k: v for k, v in results.items() if not v.startswith("ok")}
    msg = f"{len(ids)} orders in this filter, {len(ids) - len(todo)} already had invoices, {ok} fetched now."
    if bad:
        msg += " Problems: " + "; ".join(f"{k}: {v}" for k, v in list(bad.items())[:4])
        if any("signed-out" in v for v in bad.values()):
            msg += " Click 'Sign in to Amazon' first."
    if proc.returncode != 0 and not results:
        err = (proc.stderr or "").strip()
        tail = [l for l in err.splitlines() if l.strip() and not l.strip().startswith(("-", "<", "Call log", "at "))]
        msg += " The fetch failed: " + (tail[-1] if tail else err[-300:])
    with db.session() as conn:
        db.kv_set(conn, "last_invoice_fetch", f"{date.today().isoformat()} ({ok} orders fetched)")
    return redirect(target, msg)


@app.get("/charges/invoices.zip")
def charges_invoices_zip(card: str = "", date_from: str = "", date_to: str = ""):
    """All fetched invoice PDFs for the filter in one zip to attach to an email (invoices only)."""
    import io, zipfile
    with db.session() as conn:
        ids = fetcher.orders_for_filter(conn, card=card or None, date_from=date_from or None, date_to=date_to or None)
        buf = io.BytesIO()
        n = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for oid in ids:
                for f in db.invoice_files(conn, oid):
                    if Path(f).exists():
                        z.write(f, Path(f).name); n += 1
    name = f"amazon-invoices-card{card or 'all'}-{date_from or 'start'}-to-{date_to or 'end'}.zip"
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/invoices/{order_id}.pdf")
def invoice_pdf(order_id: str):
    with db.session() as conn:
        o = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if not o:
            return Response("unknown order", status_code=404)
        files = [f for f in db.invoice_files(conn, order_id) if Path(f).exists()]
        if files:
            data = Path(files[0]).read_bytes()
        elif o["invoice_pdf"] and Path(db._resolve_data_path(o["invoice_pdf"])).exists():
            data = Path(db._resolve_data_path(o["invoice_pdf"])).read_bytes()
        else:
            ships = conn.execute("SELECT * FROM shipments WHERE order_id = ? ORDER BY shipped_date", (order_id,)).fetchall()
            items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
            from .invoices.summary_pdf import build_summary_pdf
            p = build_summary_pdf(export.SUMMARY_DIR / f"{order_id}_summary.pdf", order=o, shipments=ships, items=items)
            data = p.read_bytes()
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="amazon-{order_id}.pdf"'})


# ---------- export ----------

@app.get("/export/csv")
def export_csv(account: str = "", date_from: str = "", date_to: str = ""):
    with db.session() as conn:
        body = export.reconciliation_csv(conn, account=account or None, date_from=date_from or None, date_to=date_to or None)
    name = f"amazon-reconciliation-{account or 'all'}-{date.today()}.csv".replace(" ", "_")
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/export/package")
def export_package(account: str = "", date_from: str = "", date_to: str = ""):
    with db.session() as conn:
        data, stats = export.build_package(conn, account=account or None, date_from=date_from or None, date_to=date_to or None)
    name = f"amazon-invoices-{account or 'all'}-{date.today()}.zip".replace(" ", "_").replace("#", "")
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})
