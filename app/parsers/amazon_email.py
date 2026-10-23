"""Parse Amazon order / shipment / refund notification emails.

Built against the current (2025-2026) amazon.ca notification layout, which in
plain text looks like:

    Thanks for your order!            (or: Your package was shipped!)
    ...
    Order #
    701-5593638-4701836
    ...
    * <item title>
      Quantity: 1
      46.99 CAD
    ...
    Total
    52.63 CAD

Older layouts ("Order Total: CDN$ 12.34", "Grand Total: $12.34") are handled
by fallback regexes. Anything unrecognised is reported as kind="ignored".
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional

ORDER_ID_RE = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")
MARKETPLACE_RE = re.compile(r"amazon\.(com\.au|com\.mx|com\.br|co\.uk|co\.jp|ca|com|de|fr|it|es|nl|in|ae|sg)", re.I)
ITEM_RE = re.compile(
    r"^\*\s+(?P<desc>.+?)\s*\n\s*Quantity:\s*(?P<qty>\d+)\s*\n\s*(?P<price>[\d,]+\.\d{2})\s*(?P<cur>[A-Z]{3})?",
    re.M,
)
TOTAL_RE = re.compile(r"^\s*(?:Total|Order Total|Grand Total|Shipment Total|Refund Total|Refund total)\s*:?\s*\n?\s*(?P<cur1>[A-Z]{3}|CDN\$|US\$|C\$|\$|£|€)?\s*(?P<amt>[\d,]+\.\d{2})\s*(?P<cur2>[A-Z]{3})?", re.M | re.I)
SHIPMENT_ID_RE = re.compile(r"shipmentId=([A-Za-z0-9_-]+)")
INVOICE_URL_RE = re.compile(r"(https?://www\.amazon\.[a-z.]+/gp/css/summary/print\.html\?[^\s)\]\">]+)")
TRACK_URL_RE = re.compile(r"(https?://www\.amazon\.[a-z.]+/progress-tracker/package[^\s)\]\">]+)")
BUYER_RE = re.compile(r"^\s*([^\n–-]{1,60}?)\s+–\s+([^\n]+)$", re.M)

CURRENCY_SYMBOLS = {"CDN$": "CAD", "C$": "CAD", "US$": "USD", "$": None, "£": "GBP", "€": "EUR"}
MARKETPLACE_CURRENCY = {"amazon.ca": "CAD", "amazon.com": "USD", "amazon.co.uk": "GBP", "amazon.de": "EUR",
                        "amazon.fr": "EUR", "amazon.it": "EUR", "amazon.es": "EUR", "amazon.nl": "EUR",
                        "amazon.com.au": "AUD", "amazon.co.jp": "JPY", "amazon.in": "INR", "amazon.com.mx": "MXN"}


@dataclass
class ParsedItem:
    description: str
    quantity: int
    unit_price: Optional[float]
    currency: Optional[str]


@dataclass
class ParsedOrder:
    order_id: str
    order_date: Optional[str]
    total: Optional[float]
    currency: Optional[str]
    marketplace: Optional[str]
    buyer: Optional[str] = None
    invoice_url: Optional[str] = None
    source_message_id: Optional[str] = None
    subject: Optional[str] = None
    items: list[ParsedItem] = field(default_factory=list)


@dataclass
class ParsedShipment:
    shipment_id: str
    order_id: str
    shipped_date: Optional[str]
    total: Optional[float]
    currency: Optional[str]
    marketplace: Optional[str]
    tracking_url: Optional[str] = None
    invoice_url: Optional[str] = None
    source_message_id: Optional[str] = None
    items: list[ParsedItem] = field(default_factory=list)


@dataclass
class ParsedRefund:
    refund_id: str
    order_id: str
    refund_date: Optional[str]
    total: Optional[float]
    currency: Optional[str]
    marketplace: Optional[str]
    source_message_id: Optional[str] = None


@dataclass
class ParseResult:
    kind: str                      # order | shipment | refund | ignored
    reason: str = ""
    order: Optional[ParsedOrder] = None
    shipment: Optional[ParsedShipment] = None
    refund: Optional[ParsedRefund] = None


# ---------- helpers ----------

class _TextExtractor(HTMLParser):
    """Very small HTML -> text converter that keeps line structure."""
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0
        self._href = None

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "td":
            self.parts.append(" ")
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self._href = v

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href:
            self.parts.append(f" {self._href} ")
            self._href = None

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw)
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t​͏ ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _amount(s: str) -> float:
    return float(s.replace(",", ""))


def _iso_date(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value[:len(fmt) + 6], fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return None


def _marketplace(sender: str, text: str) -> Optional[str]:
    for src in (sender or "", text or ""):
        m = MARKETPLACE_RE.search(src)
        if m:
            return "amazon." + m.group(1).lower()
    return None


def _total(text: str, default_currency: Optional[str]):
    matches = list(TOTAL_RE.finditer(text))
    if not matches:
        return None, default_currency
    m = matches[-1]  # the last "Total" block is the grand total
    cur = m.group("cur2") or m.group("cur1")
    if cur in CURRENCY_SYMBOLS:
        cur = CURRENCY_SYMBOLS[cur] or default_currency
    return _amount(m.group("amt")), cur or default_currency


def _items(text: str, default_currency: Optional[str]) -> list[ParsedItem]:
    out = []
    for m in ITEM_RE.finditer(text):
        out.append(ParsedItem(
            description=m.group("desc").strip(),
            quantity=int(m.group("qty")),
            unit_price=_amount(m.group("price")),
            currency=m.group("cur") or default_currency,
        ))
    return out


def classify(subject: str, text: str) -> str:
    s = (subject or "").lower()
    t = (text or "")[:4000].lower()
    if "refund" in s or "refund" in t[:1500]:
        return "refund"
    if s.startswith("shipped") or "your package was shipped" in t or "has shipped" in s or "shipped:" in s:
        return "shipment"
    if s.startswith("ordered") or "thanks for your order" in t or s.startswith("your amazon") and "order" in s and "shipped" not in s:
        return "order"
    if "order confirmation" in s:
        return "order"
    return "ignored"


# ---------- main entry ----------

def parse_message(*, subject: str, sender: str, date_header, body_text: Optional[str] = None,
                  body_html: Optional[str] = None, message_id: Optional[str] = None) -> ParseResult:
    text = body_text or ""
    if not text.strip() and body_html:
        text = html_to_text(body_html)
    text = text.replace("\r\n", "\n")

    kind = classify(subject, text)
    if kind == "ignored":
        return ParseResult("ignored", "not an order/shipment/refund email")

    oid = ORDER_ID_RE.search(text) or ORDER_ID_RE.search(subject or "")
    if not oid:
        return ParseResult("ignored", "no order number found")
    order_id = oid.group(1)

    marketplace = _marketplace(sender, text)
    default_cur = MARKETPLACE_CURRENCY.get(marketplace or "", None)
    total, currency = _total(text, default_cur)
    items = _items(text, currency)
    msg_date = _iso_date(date_header)
    inv = INVOICE_URL_RE.search(text)
    invoice_url = html.unescape(inv.group(1)) if inv else None

    if kind == "order":
        buyer = None
        bm = BUYER_RE.search(text)
        if bm and "amazon" not in bm.group(1).lower():
            buyer = bm.group(1).strip()
        order = ParsedOrder(order_id=order_id, order_date=msg_date, total=total, currency=currency,
                            marketplace=marketplace, buyer=buyer, invoice_url=invoice_url,
                            source_message_id=message_id, subject=subject, items=items)
        return ParseResult("order", order=order)

    if kind == "shipment":
        sm = SHIPMENT_ID_RE.search(text)
        tm = TRACK_URL_RE.search(text)
        shipment_id = sm.group(1) if sm else f"{order_id}-S{(message_id or msg_date or 'x')[-8:]}"
        ship = ParsedShipment(shipment_id=shipment_id, order_id=order_id, shipped_date=msg_date, total=total,
                              currency=currency, marketplace=marketplace,
                              tracking_url=html.unescape(tm.group(1)) if tm else None,
                              invoice_url=invoice_url, source_message_id=message_id, items=items)
        return ParseResult("shipment", shipment=ship)

    refund = ParsedRefund(refund_id=f"{order_id}-R{(message_id or msg_date or 'x')[-12:]}", order_id=order_id,
                          refund_date=msg_date, total=total, currency=currency, marketplace=marketplace,
                          source_message_id=message_id)
    if total is None:
        return ParseResult("ignored", "refund email without an amount")
    return ParseResult("refund", refund=refund)


def parse_eml(raw_bytes: bytes, message_id: Optional[str] = None) -> ParseResult:
    """Parse a raw RFC822 message (.eml file)."""
    import email
    from email import policy

    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    return parse_message(
        subject=msg.get("Subject", "") or "",
        sender=msg.get("From", "") or "",
        date_header=msg.get("Date"),
        body_text=text_part.get_content() if text_part else None,
        body_html=html_part.get_content() if html_part else None,
        message_id=message_id or (msg.get("Message-ID") or "").strip("<>") or None,
    )
