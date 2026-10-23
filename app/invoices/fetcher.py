"""Download Amazon's official invoice PDFs with a real browser session.

Amazon has no API for personal accounts (and the Business API needs weeks of
developer approval), so the practical way to get the official invoice is the
"Print invoice" page (gp/css/summary/print.html). This module drives Chromium
through Playwright using a *persistent* browser profile:

  1. `python -m app.cli amazon-login`  opens a browser window; sign in to
     Amazon normally (password, 2FA...). Close the window when you see Your Orders.
  2. From then on, "Download invoices" visits each order's invoice page in that
     logged-in profile and saves it as a PDF. No credentials are stored by this app.

If Amazon shows a sign-in page or a captcha, the download for that order is
skipped and reported; run amazon-login again.

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import config

PROFILE_DIR = Path(os.environ.get("RECON_BROWSER_PROFILE", config.DATA_DIR / "browser-profile"))
INVOICE_DIR = Path(os.environ.get("RECON_INVOICE_DIR", config.DATA_DIR / "invoices"))


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _context(pw, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = dict(headless=headless, viewport={"width": 1200, "height": 900})
    exe = os.environ.get("RECON_CHROMIUM_PATH")
    if exe:
        kwargs["executable_path"] = exe
    return pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)


def interactive_login(marketplace: str = "amazon.ca") -> None:
    """Open a visible browser on the Amazon sign-in page and wait until it is closed."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"https://www.{marketplace}/gp/css/order-history", wait_until="domcontentloaded")
        print("Sign in to Amazon in the browser window. Close the window when you can see 'Your Orders'.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


def _looks_signed_out(page) -> bool:
    url = page.url.lower()
    if "/ap/signin" in url or "/ap/cvf" in url or "captcha" in url:
        return True
    title = (page.title() or "").lower()
    return "sign in" in title or "sign-in" in title


def download_invoice(order_id: str, invoice_url: str | None, marketplace: str | None, *, headless: bool = True,
                     out_dir: Path = INVOICE_DIR) -> tuple[Path | None, str]:
    """Save the official invoice for one order. Returns (path, status)."""
    from playwright.sync_api import sync_playwright

    url = invoice_url or f"https://www.{marketplace or 'amazon.ca'}/gp/css/summary/print.html?orderID={order_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"amazon-invoice-{order_id}.pdf"
    with sync_playwright() as pw:
        ctx = _context(pw, headless=headless)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=60_000)
            if _looks_signed_out(page):
                return None, "signed-out"
            if order_id not in page.content():
                return None, "unexpected-page"
            page.emulate_media(media="print")
            page.pdf(path=str(target), format="Letter", print_background=True,
                     margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
            return target, "ok"
        except Exception as exc:
            return None, f"error: {exc}"[:200]
        finally:
            ctx.close()


def download_many(conn, order_ids: list[str], *, headless: bool = True) -> dict[str, str]:
    results = {}
    for oid in order_ids:
        row = conn.execute("SELECT order_id, invoice_url, marketplace FROM orders WHERE order_id = ?", (oid,)).fetchone()
        if not row:
            results[oid] = "unknown order"
            continue
        path, status = download_invoice(row["order_id"], row["invoice_url"], row["marketplace"], headless=headless)
        if path:
            conn.execute("UPDATE orders SET invoice_pdf = ? WHERE order_id = ?", (str(path), oid))
            conn.commit()
        results[oid] = status
        if status == "signed-out":
            break  # no point continuing until the user logs in again
    return results
