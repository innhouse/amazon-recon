"""Parse a pasted list of charges such as the one accounting sends by email:

    Oct 17, 2026   52.63
    Oct 14, 2026   AMZN Mktp CA  105.26
    2026-10-13
    10/11/2026 $19.99

Each non-empty line becomes one bank transaction. The amount is optional; a
line with only a date becomes a date-only request that is matched by date.
"""
from __future__ import annotations

import hashlib
import re

from .. import config
from .importer import BankTxn, parse_amount, parse_date

DATE_TOKEN_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4})",
    re.I,
)
AMOUNT_TOKEN_RE = re.compile(r"(?<![\d/-])(-?\(?\$?\s*-?\d{1,3}(?:,\d{3})*\.\d{2}\)?(?:\s*CR)?)(?![\d/-])", re.I)


def _norm_date(token: str) -> str | None:
    t = re.sub(r"(?<=[A-Za-z])\.", "", token.strip())
    t = re.sub(r"\bSept\b", "Sep", t)
    t = re.sub(r"\s+", " ", t)
    if t[0].isalpha():
        t = re.sub(r"^([A-Za-z]{3})[A-Za-z]*", r"\1", t)
        if "," not in t:
            parts = t.split(" ")
            t = f"{parts[0]} {parts[1]}, {parts[2]}"
    else:
        m = re.match(r"^(\d{1,2}) ([A-Za-z]{3})[A-Za-z]*,? (\d{4})$", t)
        if m:
            t = f"{m.group(2)} {m.group(1)}, {m.group(3)}"
    return parse_date(t)


def parse_quick_lines(text: str, *, account: str, currency: str = "CAD", source_file: str = "pasted") -> tuple[list[BankTxn], list[str]]:
    amazon_re = re.compile(config.AMAZON_DESCRIPTION_REGEX, re.I)
    txns, errors = [], []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        dm = DATE_TOKEN_RE.search(line)
        if not dm:
            errors.append(f"line {n}: no date found in {line!r}")
            continue
        d = _norm_date(dm.group(1))
        if not d:
            errors.append(f"line {n}: unreadable date {dm.group(1)!r}")
            continue
        rest = (line[:dm.start()] + " " + line[dm.end():]).strip()
        am = AMOUNT_TOKEN_RE.search(rest)
        amount = None
        if am:
            tok = am.group(1)
            amount = parse_amount(re.sub(r"\s*CR$", "", tok, flags=re.I))
            if amount is not None:
                amount = abs(amount) * (-1 if (tok.upper().endswith("CR") or tok.startswith("(") or tok.startswith("-")) else 1)
            rest = (rest[:am.start()] + " " + rest[am.end():]).strip()
        desc = re.sub(r"\s+", " ", rest).strip(" -|") or "Amazon charge (pasted)"
        amt_s = f"{amount:.2f}" if amount is not None else "?"
        fp = hashlib.sha1(f"{account}|{d}|{desc.lower()}|{amt_s}|{n}".encode()).hexdigest()
        txns.append(BankTxn(txn_date=d, description=desc, amount=amount, currency=currency, account=account,
                            source_file=source_file, raw_row=[raw], fingerprint=fp,
                            is_amazon=True if not rest or amazon_re.search(desc) else True))
    return txns, errors
