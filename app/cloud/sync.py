"""Upload the local data (orders, charges, refunds, invoice PDFs) to Firebase.

Firestore: a separate named database "amazon-recon" in the Google Cloud project,
so its security rules are independent of the other InnHouse apps.
Cloud Storage: the bucket given in RECON_BUCKET (default innprop-amazon-recon-data),
PDFs under invoices/<order_id>/<file name>.

Credentials: Application Default Credentials on the PC
    gcloud auth application-default login
(or GOOGLE_APPLICATION_CREDENTIALS pointing at a service-account key).

Run:  python -m app.cli sync            (idempotent; re-run after every import / fetch)
      python -m app.cli sync --dry-run  (show what would be written, no credentials needed)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import config, db

PROJECT = os.environ.get("RECON_GCP_PROJECT", "innprop-innhouse")
DATABASE = os.environ.get("RECON_FIRESTORE_DB", "amazon-recon")
BUCKET = os.environ.get("RECON_BUCKET", "innprop-amazon-recon-data")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_payload(conn) -> dict:
    """Everything to upload, as plain dicts keyed by document id. Pure function: easy to test."""
    orders, charges, refunds = {}, {}, {}
    items_by_order: dict[str, list] = {}
    items_by_shipment: dict[str, list] = {}
    for it in conn.execute("SELECT order_id, shipment_id, description, quantity, unit_price, currency FROM order_items ORDER BY id"):
        rec = {"description": it["description"], "quantity": it["quantity"], "unit_price": it["unit_price"], "currency": it["currency"]}
        if it["shipment_id"]:
            items_by_shipment.setdefault(it["shipment_id"], []).append(rec)
        else:
            items_by_order.setdefault(it["order_id"], []).append(rec)

    files_by_order: dict[str, list] = {}
    for oid in [r[0] for r in conn.execute("SELECT DISTINCT order_id FROM invoice_files")]:
        for f in db.invoice_files(conn, oid):
            p = Path(f)
            if p.exists():
                files_by_order.setdefault(oid, []).append({"local": str(p), "name": p.name, "storage": f"invoices/{oid}/{p.name}"})

    for o in conn.execute("SELECT * FROM orders"):
        oid = o["order_id"]
        orders[oid] = {
            "order_id": oid, "order_date": o["order_date"], "total": o["total"], "currency": o["currency"],
            "marketplace": o["marketplace"], "buyer": o["buyer"], "invoice_url": o["invoice_url"],
            "items": items_by_order.get(oid, []),
            "invoice_files": [f["storage"] for f in files_by_order.get(oid, [])],
        }
    for s in conn.execute("SELECT * FROM shipments"):
        sid = s["shipment_id"]
        charges[sid] = {
            "charge_id": sid, "order_id": s["order_id"], "date": s["shipped_date"], "amount": s["total"],
            "currency": s["currency"], "card": s["card_last4"], "code": s["charge_code"], "payer": s["payer"],
            "source": s["source"], "items": items_by_shipment.get(sid, []),
            "invoice_files": [f["storage"] for f in files_by_order.get(s["order_id"], [])],
        }
    for r in conn.execute("SELECT * FROM refunds"):
        refunds[r["refund_id"]] = {
            "refund_id": r["refund_id"], "order_id": r["order_id"], "date": r["refund_date"], "amount": r["total"],
            "currency": r["currency"], "card": r["card_last4"], "code": r["charge_code"], "source": r["source"],
        }
    cards = sorted({c["card"] for c in charges.values() if c["card"]})
    summary = {
        "orders": len(orders), "charges": len(charges), "refunds": len(refunds),
        "orders_with_invoices": sum(1 for o in orders.values() if o["invoice_files"]),
        "pdfs": sum(len(v) for v in files_by_order.values()),
        "cards": cards, "last_report_import": db.kv_get(conn, "last_report_import"),
        "last_invoice_fetch": db.kv_get(conn, "last_invoice_fetch"), "last_sync": _now(),
        "date_from": min((c["date"] for c in charges.values() if c["date"]), default=None),
        "date_to": max((c["date"] for c in charges.values() if c["date"]), default=None),
    }
    return {"orders": orders, "charges": charges, "refunds": refunds, "files": files_by_order, "summary": summary}


def _clients():
    from google.cloud import firestore, storage
    fs = firestore.Client(project=PROJECT, database=DATABASE)
    st = storage.Client(project=PROJECT)
    return fs, st.bucket(BUCKET)


def upload(conn, *, dry_run: bool = False, log=print) -> dict:
    payload = build_payload(conn)
    stats = {"orders": len(payload["orders"]), "charges": len(payload["charges"]), "refunds": len(payload["refunds"]),
             "pdfs_total": sum(len(v) for v in payload["files"].values()), "pdfs_uploaded": 0, "pdfs_skipped": 0}
    if dry_run:
        log(f"[dry run] would write {stats['orders']} orders, {stats['charges']} charges, {stats['refunds']} refunds "
            f"and upload {stats['pdfs_total']} PDFs to gs://{BUCKET} (Firestore db '{DATABASE}' in {PROJECT})")
        return stats

    fs, bucket = _clients()
    # PDFs first (skip ones already in the bucket)
    for oid, files in payload["files"].items():
        for f in files:
            blob = bucket.blob(f["storage"])
            if blob.exists():
                stats["pdfs_skipped"] += 1
                continue
            blob.upload_from_filename(f["local"], content_type="application/pdf")
            stats["pdfs_uploaded"] += 1
    log(f"PDFs: {stats['pdfs_uploaded']} uploaded, {stats['pdfs_skipped']} already there")

    # Firestore documents in batches of 400
    def write_all(collection, docs):
        batch, n = fs.batch(), 0
        for doc_id, data in docs.items():
            batch.set(fs.collection(collection).document(doc_id), data)
            n += 1
            if n % 400 == 0:
                batch.commit(); batch = fs.batch()
        if n % 400:
            batch.commit()
        log(f"{collection}: {n} documents")

    write_all("orders", payload["orders"])
    write_all("charges", payload["charges"])
    write_all("refunds", payload["refunds"])
    fs.collection("meta").document("summary").set(payload["summary"])
    log(f"summary written; last_sync {payload['summary']['last_sync']}")
    return stats
