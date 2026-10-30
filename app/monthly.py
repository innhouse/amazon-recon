"""Monthly package per card: one zip of the month's invoice PDFs + a CSV of the statement lines,
emailed to accounting (one email per card).

    python -m app.cli monthly                      # previous calendar month, all cards, write zips to data/monthly/
    python -m app.cli monthly --month 2026-10      # a specific month
    python -m app.cli monthly --card 3874 --send   # email it (SMTP settings below)
    python -m app.cli monthly --draft              # leave the emails as .eml files to review instead of sending

Email settings (environment variables):
    RECON_MAIL_TO       e.g. wendy@innhouse.bc.ca
    RECON_MAIL_CC       e.g. amin@innhouse.bc.ca
    RECON_SMTP_USER     the Google Workspace address that sends (e.g. amin@innhouse.bc.ca)
    RECON_SMTP_PASSWORD a Google "app password" for that address (Google Account -> Security -> App passwords)
    RECON_SMTP_HOST     default smtp.gmail.com, RECON_SMTP_PORT default 587
"""
from __future__ import annotations

import calendar
import csv
import io
import os
import smtplib
import zipfile
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from . import config, db


@dataclass
class CardPackage:
    card: str
    month: str                      # YYYY-MM
    charges: list                   # rows (dicts) in date order
    refunds: list
    zip_bytes: bytes
    csv_text: str
    pdf_count: int
    missing_orders: list = field(default_factory=list)   # orders with no stored invoice

    @property
    def total(self) -> float:
        return round(sum(c["amount"] or 0 for c in self.charges) - sum(r["amount"] or 0 for r in self.refunds), 2)

    @property
    def filename(self) -> str:
        return f"amazon-invoices-card{self.card}-{self.month}.zip"


def previous_month(today: date | None = None) -> str:
    today = today or date.today()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def build_packages(conn, month: str, cards: list[str] | None = None) -> list[CardPackage]:
    start, end = month_bounds(month)
    if cards is None:
        cards = [r[0] for r in conn.execute(
            "SELECT DISTINCT card_last4 FROM shipments WHERE card_last4 IS NOT NULL AND shipped_date BETWEEN ? AND ? ORDER BY 1", (start, end))]
    packages = []
    for card in cards:
        charges = [dict(r) for r in conn.execute(
            """SELECT s.shipped_date AS date, s.total AS amount, s.currency, s.charge_code AS code, s.order_id, s.shipment_id
               FROM shipments s WHERE s.card_last4 = ? AND s.shipped_date BETWEEN ? AND ? ORDER BY s.shipped_date, s.order_id""",
            (card, start, end))]
        refunds = [dict(r) for r in conn.execute(
            """SELECT refund_date AS date, total AS amount, currency, charge_code AS code, order_id, refund_id
               FROM refunds WHERE card_last4 = ? AND refund_date BETWEEN ? AND ? ORDER BY refund_date""", (card, start, end))]
        if not charges and not refunds:
            continue
        for c in charges:
            it = conn.execute("SELECT description, quantity FROM order_items WHERE shipment_id = ? ORDER BY id LIMIT 1", (c["shipment_id"],)).fetchone() \
                 or conn.execute("SELECT description, quantity FROM order_items WHERE order_id = ? ORDER BY id LIMIT 1", (c["order_id"],)).fetchone()
            c["item"] = f"{it['quantity']}x {it['description']}" if it else ""
        buf = io.BytesIO()
        pdf_count, missing, added = 0, [], set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for oid in dict.fromkeys(c["order_id"] for c in charges):
                files = [f for f in db.invoice_files(conn, oid) if Path(f).exists()]
                if not files:
                    missing.append(oid)
                for f in files:
                    name = f"{oid}/{Path(f).name}"
                    if name not in added:
                        z.write(f, name); added.add(name); pdf_count += 1
            out = io.StringIO(); w = csv.writer(out)
            w.writerow(["charge_date", "card", "statement_code", "amount", "currency", "order_id", "item", "invoice_stored"])
            for c in charges:
                w.writerow([c["date"], card, f"AMAZON* {c['code']}" if c["code"] else "", f"{c['amount']:.2f}", c["currency"], c["order_id"], c["item"],
                            "no" if c["order_id"] in missing else "yes"])
            for r in refunds:
                w.writerow([r["date"], card, f"AMAZON* {r['code']}" if r["code"] else "", f"-{r['amount']:.2f}", r["currency"], r["order_id"], "refund", ""])
            csv_text = out.getvalue()
            z.writestr(f"statement-lines-card{card}-{month}.csv", csv_text)
        packages.append(CardPackage(card=card, month=month, charges=charges, refunds=refunds, zip_bytes=buf.getvalue(),
                                    csv_text=csv_text, pdf_count=pdf_count, missing_orders=missing))
    return packages


def email_body(p: CardPackage) -> str:
    y, m = int(p.month[:4]), int(p.month[5:7])
    name = f"{calendar.month_name[m]} {y}"
    lines = [f"Hi Wendy,", "",
             f"Attached are the Amazon invoices for card #{p.card} for {name}: {len(p.charges)} charges"
             + (f" and {len(p.refunds)} refunds" if p.refunds else "") + f", net {p.total:,.2f} CAD, {p.pdf_count} invoice PDFs.",
             "The zip also contains a spreadsheet listing each statement line with its order number.", ""]
    if p.missing_orders:
        lines += [f"Invoices not yet available for {len(p.missing_orders)} order(s): {', '.join(p.missing_orders)}. They will follow.", ""]
    lines += ["Statement lines:"]
    for c in p.charges:
        lines.append(f"  {c['date']}  {('AMAZON* ' + c['code']) if c['code'] else '':<20} {c['amount']:>10,.2f}  {c['order_id']}")
    for r in p.refunds:
        lines.append(f"  {r['date']}  {('AMAZON* ' + r['code']) if r['code'] else '':<20} {-r['amount']:>10,.2f}  {r['order_id']} (refund)")
    lines += ["", "Thanks,", "Amin"]
    return "\n".join(lines)


def build_message(p: CardPackage, *, to: str, cc: str | None, sender: str) -> EmailMessage:
    y, m = int(p.month[:4]), int(p.month[5:7])
    msg = EmailMessage()
    msg["Subject"] = f"Amazon invoices - card #{p.card} - {calendar.month_name[m]} {y}"
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg.set_content(email_body(p))
    msg.add_attachment(p.zip_bytes, maintype="application", subtype="zip", filename=p.filename)
    return msg


def send(msg: EmailMessage) -> None:
    host = os.environ.get("RECON_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("RECON_SMTP_PORT", "587"))
    user = os.environ.get("RECON_SMTP_USER", "")
    password = os.environ.get("RECON_SMTP_PASSWORD", "")
    if not (user and password):
        raise RuntimeError("Set RECON_SMTP_USER and RECON_SMTP_PASSWORD (a Google app password) to send email.")
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def run(conn, *, month: str | None = None, cards: list[str] | None = None, mode: str = "files",
        out_dir: Path | None = None, log=print) -> list[CardPackage]:
    """mode: files (write zips), draft (write .eml files to review), send (email them)."""
    month = month or previous_month()
    out_dir = out_dir or (config.DATA_DIR / "monthly" / month)
    out_dir.mkdir(parents=True, exist_ok=True)
    to = os.environ.get("RECON_MAIL_TO", "")
    cc = os.environ.get("RECON_MAIL_CC", "")
    sender = os.environ.get("RECON_SMTP_USER", cc or "amin@innhouse.bc.ca")
    packages = build_packages(conn, month, cards)
    if not packages:
        log(f"No charges found for {month}.")
        return []
    for p in packages:
        (out_dir / p.filename).write_bytes(p.zip_bytes)
        note = f" ({len(p.missing_orders)} orders without invoice yet)" if p.missing_orders else ""
        log(f"card {p.card}: {len(p.charges)} charges, {p.pdf_count} PDFs, net {p.total:,.2f} -> {out_dir / p.filename}{note}")
        if mode in ("draft", "send"):
            if not to:
                raise RuntimeError("Set RECON_MAIL_TO (accounting's address) to draft or send emails.")
            msg = build_message(p, to=to, cc=cc or None, sender=sender)
            if mode == "draft":
                path = out_dir / f"email-card{p.card}-{month}.eml"
                path.write_bytes(bytes(msg))
                log(f"   draft written: {path} (open it in your mail program to review and send)")
            else:
                send(msg)
                log(f"   emailed to {to}" + (f" (cc {cc})" if cc else ""))
    return packages
