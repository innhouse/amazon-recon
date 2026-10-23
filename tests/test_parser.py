from pathlib import Path

from app.parsers.amazon_email import parse_eml, parse_message

FIX = Path(__file__).parent / "fixtures"


def test_order_email():
    r = parse_eml((FIX / "ordered.eml").read_bytes())
    assert r.kind == "order"
    o = r.order
    assert o.order_id == "702-1234567-8901234"
    assert o.order_date == "2026-10-06"
    assert o.total == 105.26 and o.currency == "CAD"
    assert o.marketplace == "amazon.ca"
    assert o.buyer == "Sam"
    assert [(i.quantity, i.unit_price) for i in o.items] == [(1, 46.99), (1, 46.99)]
    assert o.items[0].description.startswith("Remedy Kombucha")


def test_shipment_email_plain():
    r = parse_eml((FIX / "shipped.eml").read_bytes())
    assert r.kind == "shipment"
    s = r.shipment
    assert s.shipment_id == "Nx0QzhDs1"
    assert s.order_id == "702-1234567-8901234"
    assert s.total == 52.63
    assert s.shipped_date == "2026-10-07"
    assert "print.html?orderID=702-1234567-8901234" in s.invoice_url
    assert len(s.items) == 1


def test_shipment_email_html_only():
    r = parse_eml((FIX / "shipped_html.eml").read_bytes())
    assert r.kind == "shipment"
    assert r.shipment.shipment_id == "Ab9ZzYx2"
    assert r.shipment.total == 52.63
    assert r.shipment.items[0].description.startswith("Remedy Kombucha")


def test_ignores_noise():
    r = parse_message(subject="Delivery estimate update for your Amazon order #701-5593638-4701836", sender="no-reply@amazon.ca",
                      date_header="Wed, 21 Oct 2026 15:34:07 +0000", body_text="Delivery estimate update ... Order # 701-5593638-4701836")
    assert r.kind == "ignored"
    r = parse_message(subject="It's time to order the filter", sender="no-reply@amazon.com", date_header=None, body_text="Alexa noticed")
    assert r.kind == "ignored"


def test_legacy_order_total_format():
    body = "Hello,\nThanks for your order.\nOrder #702-1111111-2222222\nOrder Total: CDN$ 12.34\n"
    r = parse_message(subject="Your Amazon.ca order of \"Thing\"", sender="auto-confirm@amazon.ca", date_header="Mon, 01 Mar 2021 10:00:00 +0000", body_text=body)
    assert r.kind == "order" and r.order.total == 12.34 and r.order.currency == "CAD"


def test_refund_email():
    body = "Refund confirmation\nOrder # 702-1234567-8901234\nRefund total\n19.99 CAD\n"
    r = parse_message(subject="Refund on order 702-1234567-8901234", sender="auto-confirm@amazon.ca", date_header="Fri, 09 Oct 2026 10:00:00 +0000", body_text=body)
    assert r.kind == "refund" and r.refund.total == 19.99
