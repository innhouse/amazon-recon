"""Import bank / credit-card statement CSV exports.

Handles the common Canadian bank layouts:
  * headered files with Date / Description / Amount columns
  * headered files with separate Debit / Credit (or Withdrawal / Deposit) columns
  * header-less files (RBC, TD and some credit unions export these)
Column detection is automatic but the result can be overridden in the UI.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .. import config

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y", "%b %d, %Y", "%d %b %Y",
                "%B %d, %Y", "%Y%m%d", "%m/%d/%y", "%d/%m/%y", "%d-%b-%Y", "%d-%b-%y"]
AMOUNT_RE = re.compile(r"^\(?-?\$?\s*-?[\d,]*\.?\d+\)?$")


@dataclass
class Mapping:
    date: int
    description: int
    amount: Optional[int] = None      # single signed amount column
    debit: Optional[int] = None       # money out
    credit: Optional[int] = None      # money in
    amount_sign: int = 1              # 1 if positive values are charges, -1 if negative values are charges
    has_header: bool = True
    date_format: Optional[str] = None
    extra_description: list[int] = field(default_factory=list)


@dataclass
class BankTxn:
    txn_date: str
    description: str
    amount: float                     # positive = charge, negative = refund/credit
    currency: Optional[str]
    account: Optional[str]
    source_file: Optional[str]
    raw_row: list[str]
    fingerprint: str
    is_amazon: bool


def parse_date(value: str, fmt: Optional[str] = None) -> Optional[str]:
    v = (value or "").strip().strip('"')
    if not v:
        return None
    fmts = [fmt] if fmt else DATE_FORMATS
    for f in fmts:
        try:
            return datetime.strptime(v, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_amount(value: str) -> Optional[float]:
    v = (value or "").strip().replace("$", "").replace(" ", "").replace("CAD", "").replace("USD", "")
    if not v:
        return None
    neg = v.startswith("(") and v.endswith(")")
    v = v.strip("()").replace(",", "")
    if v.endswith("-"):
        neg, v = True, v[:-1]
    try:
        n = float(v)
    except ValueError:
        return None
    return -n if neg else n


def read_rows(content: bytes) -> list[list[str]]:
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    return rows


def _looks_like_header(row: list[str]) -> bool:
    if not row:
        return False
    dates = sum(1 for c in row if parse_date(c))
    nums = sum(1 for c in row if parse_amount(c) is not None and AMOUNT_RE.match(c.strip().replace(" ", "")))
    return dates == 0 and nums == 0


HEADER_HINTS = {
    "date": ["transaction date", "posted date", "post date", "date"],
    "description": ["description", "memo", "payee", "details", "narrative", "merchant", "transaction description", "name"],
    "amount": ["amount", "transaction amount", "value"],
    "debit": ["debit", "withdrawal", "withdrawals", "money out", "charge", "cad$"],
    "credit": ["credit", "deposit", "deposits", "money in", "payment"],
}


def detect_mapping(rows: list[list[str]]) -> Mapping:
    if not rows:
        raise ValueError("The file has no rows")
    header = rows[0]
    has_header = _looks_like_header(header)
    body = rows[1:] if has_header else rows
    if not body:
        raise ValueError("The file has no data rows")
    width = max(len(r) for r in body)

    def col_score(idx, fn):
        vals = [r[idx] for r in body[:50] if idx < len(r)]
        return sum(1 for v in vals if fn(v)) / max(1, len(vals))

    m = Mapping(date=-1, description=-1, has_header=has_header)
    if has_header:
        lowered = [c.strip().lower() for c in header]
        def find(keys):
            for key in keys:
                for i, h in enumerate(lowered):
                    if h == key:
                        return i
            for key in keys:
                for i, h in enumerate(lowered):
                    if key in h:
                        return i
            return None
        m.date = find(HEADER_HINTS["date"]) if find(HEADER_HINTS["date"]) is not None else -1
        m.description = find(HEADER_HINTS["description"]) if find(HEADER_HINTS["description"]) is not None else -1
        debit, credit = find(HEADER_HINTS["debit"]), find(HEADER_HINTS["credit"])
        amount = find(HEADER_HINTS["amount"])
        if debit is not None and credit is not None and debit != credit:
            m.debit, m.credit = debit, credit
        elif amount is not None:
            m.amount = amount

    # Content-based fallback for anything still unknown.
    if m.date < 0:
        scored = sorted(range(width), key=lambda i: -col_score(i, lambda v: parse_date(v) is not None))
        if scored and col_score(scored[0], lambda v: parse_date(v) is not None) > 0.8:
            m.date = scored[0]
    numeric = [i for i in range(width) if i != m.date and col_score(i, lambda v: v.strip() == "" or parse_amount(v) is not None) > 0.95
               and col_score(i, lambda v: parse_amount(v) is not None) > 0.3]
    if m.description < 0:
        texty = [i for i in range(width) if i != m.date and i not in numeric]
        if texty:
            m.description = max(texty, key=lambda i: col_score(i, lambda v: len(v.strip()) > 3))
    if m.amount is None and m.debit is None:
        # Header-less: prefer two partially-filled columns (debit/credit) else the first numeric column.
        partial = [i for i in numeric if 0.3 < col_score(i, lambda v: parse_amount(v) is not None) < 0.95]
        if len(partial) >= 2:
            m.debit, m.credit = partial[0], partial[1]
        elif numeric:
            m.amount = numeric[0]
    if m.date < 0 or m.description < 0 or (m.amount is None and m.debit is None):
        raise ValueError("Could not detect date / description / amount columns; choose them manually")

    # Decide the sign convention for a single amount column: in most card exports
    # charges are positive; in most chequing exports charges are negative. Use the
    # Amazon-looking lines to decide, else majority sign.
    if m.amount is not None:
        amazon_re = re.compile(config.AMAZON_DESCRIPTION_REGEX, re.I)
        signs = []
        for r in body:
            if m.amount < len(r) and m.description < len(r):
                a = parse_amount(r[m.amount])
                if a is not None and a != 0:
                    signs.append((amazon_re.search(r[m.description]) is not None, a > 0))
        amazon_signs = [pos for is_amz, pos in signs if is_amz]
        pool = amazon_signs or [pos for _, pos in signs]
        if pool and sum(pool) < len(pool) / 2:
            m.amount_sign = -1
    return m


def apply_mapping(rows: list[list[str]], m: Mapping, *, source_file: str = None, account: str = None,
                  currency: str = None) -> tuple[list[BankTxn], list[str]]:
    amazon_re = re.compile(config.AMAZON_DESCRIPTION_REGEX, re.I)
    body = rows[1:] if m.has_header else rows
    txns, errors = [], []
    for n, r in enumerate(body, start=2 if m.has_header else 1):
        try:
            d = parse_date(r[m.date], m.date_format)
            if not d:
                raise ValueError(f"unreadable date {r[m.date]!r}")
            desc = " ".join(filter(None, [r[m.description].strip()] + [r[i].strip() for i in m.extra_description if i < len(r)]))
            if m.amount is not None:
                a = parse_amount(r[m.amount])
                if a is None:
                    raise ValueError("missing amount")
                amount = a * m.amount_sign
            else:
                debit = parse_amount(r[m.debit]) if m.debit is not None and m.debit < len(r) else None
                credit = parse_amount(r[m.credit]) if m.credit is not None and m.credit < len(r) else None
                if debit is None and credit is None:
                    raise ValueError("missing debit/credit")
                amount = abs(debit or 0) - abs(credit or 0)
            amount = round(amount, 2)
            fp = hashlib.sha1(f"{account or ''}|{d}|{desc.lower()}|{amount:.2f}|{n}".encode()).hexdigest()
            txns.append(BankTxn(txn_date=d, description=desc, amount=amount, currency=currency, account=account,
                                source_file=source_file, raw_row=r, fingerprint=fp,
                                is_amazon=amazon_re.search(desc) is not None))
        except Exception as exc:
            errors.append(f"row {n}: {exc}")
    return txns, errors
