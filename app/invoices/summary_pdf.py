"""Generate a one-page 'charge summary' PDF from the data in Amazon's own emails / reports.

This is NOT Amazon's official invoice. It is a clearly labelled summary that
carries the order number, items, shipment total and the link to the official
invoice page, so accounting has something to file when the official PDF has
not been downloaded yet.
"""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF


def _safe(s) -> str:
    return (str(s) if s is not None else "").encode("latin-1", "replace").decode("latin-1")


def _money(v, cur):
    return f"{v:.2f} {cur or ''}".strip() if v is not None else "n/a"


def build_summary_pdf(path: Path, *, order, shipments, items, bank_txn=None) -> Path:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _safe(f"Amazon charge summary - order {order['order_id']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, new_x="LMARGIN", new_y="NEXT", text=_safe("Generated from Amazon's order emails and reconciliation report. "
                               "This is a bookkeeping summary, not Amazon's official tax invoice. "
                               f"Official invoice: {order['invoice_url'] or 'see Your Orders on ' + (order['marketplace'] or 'amazon')}"))
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    rows = [("Marketplace", order["marketplace"]), ("Order date", order["order_date"]),
            ("Order total", _money(order["total"], order["currency"])), ("Buyer", order["buyer"])]
    if bank_txn is not None:
        rows += [("Bank account", bank_txn["account"]), ("Statement date", bank_txn["txn_date"]),
                 ("Statement amount", f"{bank_txn['amount']:.2f}" if bank_txn["amount"] is not None else "n/a"),
                 ("Statement description", bank_txn["description"])]
    for k, v in rows:
        pdf.set_font("Helvetica", "B", 11); pdf.cell(45, 7, _safe(k))
        pdf.set_font("Helvetica", "", 11); pdf.cell(0, 7, _safe(v or "-"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    for s in shipments:
        pdf.set_font("Helvetica", "B", 12)
        card = f" - card ending {s['card_last4']}" if s["card_last4"] else ""
        pdf.cell(0, 8, _safe(f"Charge {s['shipment_id']} - {s['shipped_date'] or '?'} - {_money(s['total'], s['currency'])}{card}"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for it in [i for i in items if i["shipment_id"] == s["shipment_id"]]:
            pdf.multi_cell(0, 6, new_x="LMARGIN", new_y="NEXT", text=_safe(f"  {it['quantity']} x {it['description']}  @ {_money(it['unit_price'], it['currency'])}"))
        pdf.ln(2)
    order_items = [i for i in items if i["shipment_id"] is None]
    if order_items:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Items on the order confirmation", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for it in order_items:
            pdf.multi_cell(0, 6, new_x="LMARGIN", new_y="NEXT", text=_safe(f"  {it['quantity']} x {it['description']}  @ {_money(it['unit_price'], it['currency'])}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path
