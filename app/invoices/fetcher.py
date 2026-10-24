"""Download Amazon's official invoice PDFs with a real browser session.

Amazon has no API for personal accounts, and the Business API needs weeks of
developer approval, so this drives Chromium through Playwright with a
*persistent* browser profile:

  1. `python -m app.cli amazon-login` opens a browser window; sign in to the
     Amazon account that placed the orders (Business or personal), then close
     the window.
  2. `python -m app.cli download-invoices --card 3874 --from 2026-10-07 --to 2026-10-17 --out <folder>`
     opens Your Orders searched for each order, opens its "Invoice" menu and
     saves every "Invoice N" PDF (one per shipment / card charge).

No Amazon password is stored by this app; only the browser profile in
data/browser-profile, like a normal Chrome profile.

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .. import config, db

PROFILE_DIR = Path(os.environ.get("RECON_BROWSER_PROFILE", config.DATA_DIR / "browser-profile"))
ACCOUNT = "business"   # which Amazon sign-in to use: "business" or "personal" (separate browser profiles)


def set_account(name: str) -> None:
    """Choose the Amazon account whose sign-in the browser uses. Each account has its own profile folder."""
    global ACCOUNT, PROFILE_DIR
    ACCOUNT = (name or "business").strip().lower()
    base = Path(os.environ.get("RECON_BROWSER_PROFILE", config.DATA_DIR / "browser-profile"))
    PROFILE_DIR = base if ACCOUNT == "business" else base.parent / f"{base.name}-{ACCOUNT}"
INVOICE_DIR = Path(os.environ.get("RECON_INVOICE_DIR", config.DATA_DIR / "invoices"))
INVOICE_LINK_RE = re.compile(r"^\s*Invoice\s*\d*\s*$", re.I)
WAIT_MS = int(os.environ.get("RECON_FETCH_WAIT_MS", "30000"))   # how long to wait for Amazon's Invoice menu


def _base(marketplace: str | None) -> str:
    """Site root; RECON_AMAZON_BASE_URL overrides it (used by the tests against a local fake)."""
    return os.environ.get("RECON_AMAZON_BASE_URL") or f"https://www.{marketplace or 'amazon.ca'}"


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _prepare_profile() -> None:
    """Set Chrome preferences in our profile so PDF links download instead of opening in the viewer."""
    import json
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    default = PROFILE_DIR / "Default"
    default.mkdir(parents=True, exist_ok=True)
    prefs_path = default / "Preferences"
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    except Exception:
        prefs = {}
    plugins = prefs.setdefault("plugins", {})
    download = prefs.setdefault("download", {})
    if plugins.get("always_open_pdf_externally") is not True or download.get("prompt_for_download") is not False:
        plugins["always_open_pdf_externally"] = True
        download["prompt_for_download"] = False
        try:
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
        except Exception:
            pass


def _context(pw, headless: bool):
    _prepare_profile()
    kwargs = dict(headless=headless, viewport={"width": 1280, "height": 900}, accept_downloads=True)
    exe = os.environ.get("RECON_CHROMIUM_PATH")
    if exe:
        kwargs["executable_path"] = exe
    elif headless:
        # Use the full Chromium in its headless mode rather than the separate "headless shell"
        # build: the saved sign-in profile was created by the full browser and the shell cannot open it.
        kwargs["channel"] = "chromium"
    try:
        return pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
    except Exception:
        if "channel" in kwargs:
            kwargs.pop("channel")
            return pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
        raise


def interactive_login(marketplace: str = "amazon.ca") -> None:
    """Open a visible browser on Your Orders and wait until the user closes it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{_base(marketplace)}/gp/your-account/order-history", wait_until="domcontentloaded")
        print("Sign in to the Amazon account that placed the orders. When you can see 'Your Orders', close the browser window.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    # Verify: reopen the profile headless and check that Your Orders loads signed in.
    with sync_playwright() as pw:
        ctx = _context(pw, headless=True)
        try:
            page = ctx.new_page()
            page.goto(f"{_base(marketplace)}/gp/your-account/order-history", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)
            if _looks_signed_out(page):
                print("NOT signed in: Amazon still shows the sign-in page. Run amazon-login again and complete the sign-in before closing the window.")
            else:
                text = page.locator("body").inner_text()[:4000].lower()
                who = "business" if "business" in text else "account"
                print(f"Signed in OK ({who} account detected). Page title: {page.title()!r}")
        finally:
            ctx.close()


def _is_invoice_link(text: str, href: str) -> bool:
    if not href or href.startswith("javascript") or href == "#":
        return False
    t = (text or "").strip().lower()
    if "request" in t or "summary" in t:
        return False
    if t == "invoice":
        # Single-shipment orders label the document link just "Invoice"; the menu
        # button has the same text but no document address.
        return "/documents/" in href or href.lower().endswith(".pdf")
    return bool(INVOICE_LINK_RE.match(text or "")) or "invoice" in href.lower()


def _popover_source_links(ctx, page, base: str) -> list[tuple[str, str]]:
    """Amazon fills the Invoice menu from a separate URL (data-a-popover JSON on the trigger).
    Fetch that URL directly and read every invoice link from its HTML."""
    import json
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self):
            super().__init__(); self.items = []; self._href = None; self._text = []
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self._href = dict(attrs).get("href"); self._text = []
        def handle_data(self, data):
            if self._href is not None:
                self._text.append(data)
        def handle_endtag(self, tag):
            if tag == "a" and self._href is not None:
                self.items.append(("".join(self._text).strip(), self._href)); self._href = None

    out = []
    for el in page.locator("[data-a-popover]").all():
        try:
            raw = el.get_attribute("data-a-popover") or ""
            cfg = json.loads(raw)
            url = cfg.get("url")
            if not url or "invoice" not in url.lower():
                continue
            full = url if url.startswith("http") else f"{base}{url}"
            resp = ctx.request.get(full, timeout=30_000)
            if not resp.ok:
                continue
            parser = Links(); parser.feed(resp.text())
            for text, href in parser.items:
                if _is_invoice_link(text, href):
                    out.append((text, href if href.startswith("http") else f"{base}{href}"))
        except Exception:
            continue
    return out


URL_IN_ATTR_RE = re.compile(r"""(https?://[^\s'"<>]+|/(?:documents|gp|b2b|-)[^\s'"<>]+)""")


def _anchor_url(a) -> str:
    """Best URL for an anchor: href, else any URL-looking value in its other attributes."""
    try:
        href = a.get_attribute("href") or ""
    except Exception:
        return ""
    if href and not href.startswith("javascript") and href != "#":
        return href
    for attr in ("data-url", "data-href", "data-action-url", "data-a-popover", "onclick", "data-csa-c-href"):
        try:
            v = a.get_attribute(attr) or ""
        except Exception:
            continue
        m = URL_IN_ATTR_RE.search(v.replace("\\/", "/"))
        if m:
            return m.group(1)
    return ""


def _collect_invoice_links(page, base: str, ctx=None, raw_dump: list | None = None) -> list[tuple[str, str]]:
    """All links that look like invoices: from the menu's source URL, the page, and any iframes.
    Links with no usable address are returned with an empty URL so they can be clicked instead."""
    out, seen = [], set()

    def add(text, href):
        if not href:
            key = ("click", text)
            if key not in seen:
                seen.add(key)
                out.append((text, ""))
            return
        full = href if href.startswith("http") else f"{base}{href}"
        if full not in seen:
            seen.add(full)
            out.append((text or f"Invoice {len(out) + 1}", full))

    frames = [page] + [f for f in page.frames if f != page.main_frame]
    for scope in frames:
        try:
            anchors = scope.locator("a").all()
        except Exception:
            continue
        for a in anchors:
            try:
                text = (a.inner_text() or "").strip()
            except Exception:
                continue
            if not INVOICE_LINK_RE.match(text) or text.lower() == "invoice":
                # not an "Invoice N" label; still accept by address if it looks like an invoice
                url = _anchor_url(a)
                if url and _is_invoice_link(text, url):
                    add(text, url)
                continue
            url = _anchor_url(a)
            if raw_dump is not None and len(raw_dump) < 4:
                try:
                    raw_dump.append(a.evaluate("e => e.outerHTML")[:600])
                except Exception:
                    pass
            add(text, url)
    if ctx is not None:
        # The menu's source URL is a fallback for invoices the rendered menu did not show.
        for text, href in _popover_source_links(ctx, page, base):
            add(text, href)
    # Amazon offers each invoice through several equivalent links (menu source and rendered
    # menu, each with its own one-time address). Keep one address per "Invoice N" label.
    with_url = {t for t, u in out if u}
    deduped, labels_seen = [], set()
    for t, u in out:
        if not u and t in with_url:
            continue
        key = re.sub(r"\s+", " ", t.strip().lower())
        if INVOICE_LINK_RE.match(t) and key in labels_seen:
            continue
        labels_seen.add(key)
        deduped.append((t, u))
    return deduped


def _debug_links(out_dir: Path, order_id: str, hrefs) -> None:
    try:
        d = out_dir / "debug"; d.mkdir(parents=True, exist_ok=True)
        (d / f"{order_id}-links.txt").write_text("\n".join(f"{t}\t{h}" for t, h in hrefs) or "(no links)", encoding="utf-8")
    except Exception:
        pass


def _save_pdf_by_clicking(ctx, page, base: str, order_id: str, label: str, target: Path) -> tuple[bool, str]:
    """For menu entries without a plain address: click the entry and capture what Amazon
    returns (a file download, a new tab, or a navigation to a PDF)."""
    pattern = re.compile(r"^\s*" + re.escape(label.strip()) + r"\s*$")
    try:
        link = page.locator("a", has_text=pattern).first
        if link.count() == 0:
            # menu closed? reopen it
            trig = _wait_for_invoice_trigger(page, 10_000)
            if trig is not None:
                trig.click(); page.wait_for_timeout(1500)
            link = page.locator("a", has_text=pattern).first
            if link.count() == 0:
                return False, "menu entry not found for clicking"
        # 1. file download
        try:
            with page.expect_download(timeout=8_000) as dl:
                link.click()
            dl.value.save_as(str(target))
            return True, ""
        except Exception:
            pass
        # 2. new tab
        try:
            with ctx.expect_page(timeout=4_000) as newp:
                pass
            p2 = newp.value
            p2.wait_for_load_state("domcontentloaded", timeout=30_000)
            url = p2.url
            p2.close()
            if url and url != "about:blank":
                return _save_pdf_from_link(ctx, page, url, target)
        except Exception:
            pass
        # 3. navigation in the same tab
        page.wait_for_timeout(2500)
        url = page.url
        if "order-history" not in url and "order-details" not in url:
            ok, why = _save_pdf_from_link(ctx, page, url, target)
            try:
                page.go_back(wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1500)
                trig = _wait_for_invoice_trigger(page, 15_000)
                if trig is not None:
                    trig.click(); page.wait_for_timeout(1500)
            except Exception:
                pass
            return ok, why
        return False, "click produced no download, tab or navigation"
    except Exception as exc:
        return False, f"click error: {str(exc)[:100]}"


def _wait_for_invoice_trigger(page, timeout_ms: int):
    """Return the 'Invoice' menu trigger once it exists, or None."""
    loc = page.locator("a, span, button", has_text=re.compile(r"^\s*Invoice\s*$")).first
    try:
        loc.wait_for(state="attached", timeout=timeout_ms)
        return loc
    except Exception:
        return None


def _debug_dump(page, out_dir: Path, order_id: str) -> None:
    """Save a screenshot and the page HTML so a failure can be diagnosed."""
    try:
        d = out_dir / "debug"
        d.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(d / f"{order_id}.png"), full_page=True)
        (d / f"{order_id}.html").write_text(page.content(), encoding="utf-8")
        (d / f"{order_id}.txt").write_text(f"{page.url}\n{page.title()}\n", encoding="utf-8")
    except Exception:
        pass


def _looks_signed_out(page) -> bool:
    url = page.url.lower()
    if "/ap/signin" in url or "/ap/cvf" in url or "captcha" in url:
        return True
    title = (page.title() or "").lower()
    return "sign in" in title or "sign-in" in title


def _save_pdf_from_link(ctx, page, href: str, target: Path) -> tuple[bool, str]:
    """Save the document behind an invoice link. Amazon serves Business invoices as
    PDF files; older/personal invoices are HTML pages, which are printed to PDF.
    Returns (saved, reason-if-not)."""
    why = []
    try:
        resp = ctx.request.get(href, timeout=60_000)
        ctype = (resp.headers.get("content-type") or "").lower()
        body = resp.body()
        if resp.ok and ("pdf" in ctype or body[:5] == b"%PDF-"):
            target.write_bytes(body)
            return True, ""
        why.append(f"direct fetch gave {resp.status} {ctype[:40]}")
    except Exception as exc:
        why.append(f"direct fetch error: {str(exc)[:80]}")
    p = None
    try:
        p = ctx.new_page()
        # A link that triggers a file download fires a 'download' event instead of navigating.
        try:
            with p.expect_download(timeout=20_000) as dl_info:
                try:
                    p.goto(href, wait_until="commit", timeout=60_000)
                except Exception:
                    pass   # "Download is starting" aborts the navigation; that is fine
            dl_info.value.save_as(str(target))
            return True, ""
        except Exception as exc:
            why.append(f"no download event: {str(exc).splitlines()[0][:60]}")
        # Second option: the bytes of the navigation response itself.
        try:
            resp = p.request.get(href, timeout=60_000, headers={"Referer": f"{href.split('/documents')[0]}/gp/your-account/order-history",
                                                               "Accept": "application/pdf,*/*"})
            body = resp.body()
            if resp.ok and body[:5] == b"%PDF-":
                target.write_bytes(body)
                return True, ""
            why.append(f"referer fetch gave {resp.status}")
        except Exception as exc:
            why.append(f"referer fetch error: {str(exc)[:60]}")
        if _looks_signed_out(p):
            return False, "; ".join(why + ["signed-out"])
        try:
            p.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        p.emulate_media(media="print")
        p.pdf(path=str(target), format="Letter", print_background=True,
              margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
        return True, ""
    except Exception as exc:
        why.append(f"print error: {str(exc)[:80]}")
        return False, "; ".join(why)
    finally:
        if p is not None:
            try:
                p.close()
            except Exception:
                pass


def download_order_invoices(ctx, order_id: str, marketplace: str | None, out_dir: Path, expected: int = 1) -> tuple[list[Path], str]:
    """Fetch every invoice of one order. `expected` is how many invoices we think the order has
    (one per card charge); the menu is re-read until that many links appear or time runs out.
    Returns (saved paths, status)."""
    base = _base(marketplace)
    out_dir.mkdir(parents=True, exist_ok=True)
    page = ctx.new_page()
    try:
        page.goto(f"{base}/gp/your-account/order-history?search={order_id}", wait_until="domcontentloaded", timeout=60_000)
        # Wait until either the order card or a "no orders" notice is on the page (up to 15 s).
        try:
            page.wait_for_function(
                "(oid) => document.body && (document.body.innerText.includes(oid) || /no orders|0 orders|didn't find|sign in|sign-in/i.test(document.body.innerText))",
                arg=order_id, timeout=min(WAIT_MS, 15_000))
        except Exception:
            pass
        page.wait_for_timeout(800)
        if _looks_signed_out(page):
            _debug_dump(page, out_dir, order_id)
            return [], "signed-out"
        body_text = page.locator("body").inner_text() if page.locator("body").count() else ""
        if order_id not in body_text:
            _debug_dump(page, out_dir, order_id)
            return [], f"order-not-found (page: {page.title()!r} {page.url}) - screenshot saved in {out_dir / 'debug'}"
        # Open the "Invoice" menu of the order card. On big orders the page is heavy and
        # the menu trigger shows up late, so wait for it (up to 30 s); if it never appears,
        # try the order-details page, which has the same menu.
        trigger = _wait_for_invoice_trigger(page, WAIT_MS)
        if trigger is None:
            page.goto(f"{base}/gp/your-account/order-details?orderID={order_id}", wait_until="domcontentloaded", timeout=60_000)
            trigger = _wait_for_invoice_trigger(page, WAIT_MS)
        if trigger is None:
            _debug_dump(page, out_dir, order_id)
            return [], f"no Invoice menu on the order - screenshot saved in {out_dir / 'debug'}"
        trigger.scroll_into_view_if_needed()
        trigger.click()
        # Wait for the popover with the "Invoice N" links to render (it can be loaded on demand).
        try:
            page.locator("a", has_text=INVOICE_LINK_RE).first.wait_for(timeout=min(WAIT_MS, 15_000))
        except Exception:
            pass
        page.wait_for_timeout(1500)
        raw_dump: list = []
        hrefs = _collect_invoice_links(page, base, ctx, raw_dump)
        # Big orders fill their menu gradually: keep re-reading until we have as many
        # invoice links as the order has charges, or the wait budget is spent.
        import time as _t
        deadline = _t.monotonic() + WAIT_MS / 1000.0
        while len(hrefs) < max(1, expected) and _t.monotonic() < deadline:
            page.wait_for_timeout(2000)
            raw_dump = []
            hrefs = _collect_invoice_links(page, base, ctx, raw_dump)
        try:
            d = out_dir / "debug"; d.mkdir(parents=True, exist_ok=True)
            (d / f"{order_id}-raw-links.html").write_text("\n\n".join(raw_dump) or "(none)", encoding="utf-8")
        except Exception:
            pass
        if not hrefs:
            # Maybe the click did not open the menu; try the other trigger element once more.
            try:
                page.get_by_role("link", name=re.compile(r"^\s*Invoice\s*$")).first.click(timeout=5_000)
                page.wait_for_timeout(2000)
                hrefs = _collect_invoice_links(page, base, ctx)
            except Exception:
                pass
        _debug_links(out_dir, order_id, hrefs)
        if not hrefs:
            _debug_dump(page, out_dir, order_id)
            return [], f"Invoice menu opened but no invoice links found - see {out_dir / 'debug'}"
        saved, failures, used_names = [], [], set()
        for n, (text, href) in enumerate(hrefs, start=1):
            label = re.sub(r"\D", "", text) or str(n)
            target = out_dir / f"amazon-invoice-{order_id}-{int(label):02d}.pdf"
            if target.name in used_names:
                target = out_dir / f"amazon-invoice-{order_id}-{int(label):02d}-{n}.pdf"
            used_names.add(target.name)
            if href:
                ok, why = _save_pdf_from_link(ctx, page, href, target)
            else:
                ok, why = _save_pdf_by_clicking(ctx, page, base, order_id, text, target)
            if ok:
                saved.append(target)
            else:
                failures.append(f"{text}: {why}")
        if failures:
            (out_dir / "debug").mkdir(parents=True, exist_ok=True)
            (out_dir / "debug" / f"{order_id}-failures.txt").write_text("\n".join(failures), encoding="utf-8")
        if not saved:
            _debug_dump(page, out_dir, order_id)
            return [], f"{len(hrefs)} invoice links found but none could be saved: {failures[0] if failures else ''} - see {out_dir / 'debug'}"
        return saved, "ok" if saved else "no invoice files could be saved"
    except Exception as exc:
        return [], f"error: {exc}"[:200]
    finally:
        page.close()


def orders_for_filter(conn, *, card: str | None = None, date_from: str | None = None, date_to: str | None = None) -> list[str]:
    """Order ids charged to a card within a date range, from the shipments table."""
    where, args = ["1=1"], []
    if card:
        where.append("s.card_last4 = ?"); args.append(card)
    if date_from:
        where.append("s.shipped_date >= ?"); args.append(date_from)
    if date_to:
        where.append("s.shipped_date <= ?"); args.append(date_to)
    return [r[0] for r in conn.execute(f"SELECT DISTINCT s.order_id FROM shipments s WHERE {' AND '.join(where)} ORDER BY s.order_id", args)]


def download_many(conn, order_ids: list[str], *, headless: bool = True, out_dir: Path = INVOICE_DIR) -> dict[str, str]:
    """Fetch invoices for several orders in one browser session; records files in the database."""
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as pw:
        try:
            ctx = _context(pw, headless=headless)
        except Exception as exc:
            text = str(exc)
            if "already in use" in text or "SingletonLock" in text or "lock" in text.lower():
                reason = "browser profile already in use: close the other automated Chrome window (or the sign-in window) and try again"
            elif "Executable doesn't exist" in text:
                reason = "Chromium is not installed for Playwright: run  python -m playwright install chromium"
            else:
                reason = "could not start the browser: " + text.splitlines()[0][:200]
            return {oid: reason for oid in order_ids[:1]} or {"*": reason}
        try:
            for oid in order_ids:
                row = conn.execute("SELECT order_id, marketplace FROM orders WHERE order_id = ?", (oid,)).fetchone()
                if not row:
                    results[oid] = "unknown order"
                    continue
                expected = conn.execute("SELECT COUNT(*) FROM shipments WHERE order_id = ?", (oid,)).fetchone()[0] or 1
                paths, status = download_order_invoices(ctx, oid, row["marketplace"], out_dir, expected=expected)
                if paths and len(paths) < expected:
                    status = f"{status} but only {len(paths)} of {expected} expected invoices were in the menu"
                for p in paths:
                    db.add_invoice_file(conn, oid, str(p), p.stem.rsplit("-", 1)[-1])
                if paths:
                    conn.execute("UPDATE orders SET invoice_pdf = ? WHERE order_id = ?", (str(paths[0]), oid))
                conn.commit()
                results[oid] = f"{status} ({len(paths)} files)" if paths else status
                if status == "signed-out":
                    break
        finally:
            ctx.close()
    return results


# Backwards-compatible single-order helper used by the web route.
def download_invoice(order_id: str, invoice_url: str | None, marketplace: str | None, *, headless: bool = True,
                     out_dir: Path = INVOICE_DIR) -> tuple[Path | None, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _context(pw, headless=headless)
        try:
            paths, status = download_order_invoices(ctx, order_id, marketplace, out_dir)
        finally:
            ctx.close()
    return (paths[0] if paths else None), status
