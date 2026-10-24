"""End-to-end test of the invoice fetcher against a local fake of Amazon's Your Orders page."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
HERE = Path(__file__).parent


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture()
def fake_amazon():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(HERE / "fake_amazon" / "server.py"), str(port)])
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()


def test_download_order_invoices(fake_amazon, tmp_path, monkeypatch):
    from app.invoices import fetcher
    monkeypatch.setenv("RECON_AMAZON_BASE_URL", fake_amazon)
    monkeypatch.setattr(fetcher, "WAIT_MS", 3000)
    monkeypatch.setattr(fetcher, "PROFILE_DIR", tmp_path / "profile")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            ctx = fetcher._context(pw, headless=True)
        except Exception as exc:  # no browser downloaded for this Playwright version
            pytest.skip(f"Chromium not available: {str(exc).splitlines()[0]}")
        try:
            paths, status = fetcher.download_order_invoices(ctx, "701-0000001-0000001", "amazon.ca", tmp_path / "out")
            missing, status2 = fetcher.download_order_invoices(ctx, "701-9999999-9999999", "amazon.ca", tmp_path / "out")
            single, status3 = fetcher.download_order_invoices(ctx, "701-0000002-0000002", "amazon.ca", tmp_path / "out")
        finally:
            ctx.close()
    assert status == "ok" and [p.name for p in paths] == ["amazon-invoice-701-0000001-0000001-01.pdf", "amazon-invoice-701-0000001-0000001-02.pdf"]
    assert all(p.read_bytes().startswith(b"%PDF") for p in paths)
    assert missing == [] and status2.startswith("order-not-found")
    assert status3 == "ok" and len(single) == 1 and single[0].read_bytes().startswith(b"%PDF")
