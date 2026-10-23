"""Shared ingestion logic: take a ParseResult and store it."""
from .. import db
from ..parsers.amazon_email import ParseResult


def store_result(conn, result: ParseResult, message_id: str, source: str) -> str:
    """Persist a parsed email. Returns the outcome string recorded for the message."""
    if result.kind == "order":
        db.upsert_order(conn, result.order)
        detail = result.order.order_id
    elif result.kind == "shipment":
        db.upsert_shipment(conn, result.shipment)
        detail = f"{result.shipment.order_id} / {result.shipment.shipment_id}"
    elif result.kind == "refund":
        db.upsert_refund(conn, result.refund)
        detail = result.refund.order_id
    else:
        detail = result.reason
    db.mark_processed(conn, message_id, source, result.kind, detail)
    return result.kind


class SyncStats(dict):
    def bump(self, key):
        self[key] = self.get(key, 0) + 1

    def summary(self) -> str:
        return ", ".join(f"{k}: {v}" for k, v in sorted(self.items())) or "nothing new"
