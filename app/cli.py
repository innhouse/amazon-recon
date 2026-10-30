"""Command-line entry points.

  python -m app.cli sync-imap            # read Amazon emails over IMAP (uses IMAP_* env vars)
  python -m app.cli sync-gmail           # read Amazon emails from Gmail (OAuth)
  python -m app.cli import-eml FOLDER    # import .eml files
  python -m app.cli import-bank FILE.csv --account "RBC Visa 3874"
  python -m app.cli match                # run auto-matching
  python -m app.cli amazon-login         # open a browser to sign in to Amazon once (for invoice downloads)
  python -m app.cli download-invoices    # fetch official PDFs for matched orders
  python -m app.cli package OUT.zip --account "RBC Visa 3874"
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import config, db, export
from .bank import importer
from .recon import matcher


def main(argv=None):
    p = argparse.ArgumentParser(prog="amazon-recon")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync-imap"); s.add_argument("--lookback", type=int, default=400)
    sub.add_parser("sync-gmail")
    s = sub.add_parser("import-eml"); s.add_argument("folder")
    s = sub.add_parser("import-report", help="import an Amazon Business report CSV (reconciliation, shipment-level or orders)")
    s.add_argument("file", nargs="+"); s.add_argument("--marketplace", default="amazon.ca")
    s = sub.add_parser("import-bank"); s.add_argument("file"); s.add_argument("--account", default=None); s.add_argument("--currency", default="CAD")
    sub.add_parser("match")
    s = sub.add_parser("amazon-login"); s.add_argument("--marketplace", default="amazon.ca")
    s.add_argument("--account", default="business", help="business (default) or personal: separate sign-ins")
    s = sub.add_parser("download-invoices"); s.add_argument("--all", action="store_true"); s.add_argument("--visible", action="store_true")
    s.add_argument("--card", default=None, help="only orders charged to this card (last 4 digits)")
    s.add_argument("--from", dest="date_from", default=None); s.add_argument("--to", dest="date_to", default=None)
    s.add_argument("--out", default=None, help="folder to copy the PDFs into (e.g. a folder to attach from)")
    s.add_argument("--order", action="append", default=[], help="fetch this order only (repeatable)")
    s.add_argument("--account", default="business", help="business (default) or personal: which Amazon sign-in to use")
    s = sub.add_parser("sync", help="upload orders, charges and invoice PDFs to Firebase (Firestore + Cloud Storage)")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("monthly", help="per-card zip of a month's invoices, optionally emailed to accounting")
    s.add_argument("--month", default=None, help="YYYY-MM (default: previous month)")
    s.add_argument("--card", action="append", default=None, help="only this card (repeatable)")
    s.add_argument("--send", action="store_true", help="email the packages (RECON_MAIL_TO, RECON_SMTP_*)")
    s.add_argument("--draft", action="store_true", help="write .eml drafts instead of sending")
    s.add_argument("--out", default=None, help="folder for the zips (default data/monthly/<month>)")
    s = sub.add_parser("month-end", help="the whole monthly routine: import newest Amazon report from Downloads, fetch missing invoices, sync, write (or send) the emails")
    s.add_argument("--month", default=None, help="YYYY-MM (default: previous month)")
    s.add_argument("--send", action="store_true", help="send the emails instead of writing drafts")
    s.add_argument("--report", default=None, help="report CSV to import (default: newest 'reconciliation by shipment level*.csv' in Downloads)")
    s.add_argument("--no-fetch", action="store_true"); s.add_argument("--no-sync", action="store_true")
    s = sub.add_parser("package"); s.add_argument("out"); s.add_argument("--account", default=None); s.add_argument("--from", dest="date_from", default=None); s.add_argument("--to", dest="date_to", default=None)
    a = p.parse_args(argv)

    with db.session() as conn:
        if a.cmd == "sync-imap":
            from .sources import imap_source
            env = imap_source.settings_from_env()
            if not env["user"]:
                env["user"] = input("IMAP user (full email): ")
            if not env["password"]:
                env["password"] = getpass.getpass("App-specific password: ")
            print(imap_source.sync(conn, host=env["host"], port=env["port"], user=env["user"], password=env["password"],
                                   folder=env["folder"], lookback_days=a.lookback).summary())
        elif a.cmd == "sync-gmail":
            from .sources import gmail_source
            print(gmail_source.sync(conn).summary())
        elif a.cmd == "import-eml":
            from .sources import eml_source
            print(eml_source.import_folder(conn, Path(a.folder)).summary())
        elif a.cmd == "import-report":
            import csv as _csv, io as _io
            from datetime import date as _date
            from .sources import amazon_orders_report, amazon_report
            for f in a.file:
                content = Path(f).read_bytes()
                header = next(_csv.reader(_io.StringIO(content.decode("utf-8-sig", errors="replace"))), [])
                if amazon_report.looks_like_shipment_report(header):
                    stats, errors = amazon_report.import_shipment_report(conn, content, marketplace=a.marketplace); kind = "shipment-level reconciliation"
                elif amazon_orders_report.looks_like_orders_report(header):
                    stats, errors = amazon_orders_report.import_orders_report(conn, content, marketplace=a.marketplace); kind = "orders"
                else:
                    stats, errors = amazon_report.import_report(conn, content, marketplace=a.marketplace); kind = "reconciliation"
                db.kv_set(conn, "last_report_import", _date.today().isoformat()); conn.commit()
                print(f"{Path(f).name}: {kind} report, {stats.summary()}" + (f", {len(errors)} rows skipped" if errors else ""))
            print(matcher.run_auto_match(conn))
        elif a.cmd == "import-bank":
            rows = importer.read_rows(Path(a.file).read_bytes())
            m = importer.detect_mapping(rows)
            txns, errors = importer.apply_mapping(rows, m, source_file=a.file, account=a.account, currency=a.currency)
            n = sum(db.insert_bank_transaction(conn, t) for t in txns)
            print(f"mapping: {m}\ninserted {n} of {len(txns)} rows; {len(errors)} errors")
            for e in errors[:10]:
                print("  ", e)
            print(matcher.run_auto_match(conn))
        elif a.cmd == "match":
            print(matcher.run_auto_match(conn))
        elif a.cmd == "amazon-login":
            from .invoices import fetcher
            fetcher.set_account(a.account)
            print(f"Amazon account: {fetcher.ACCOUNT}")
            fetcher.interactive_login(a.marketplace)
        elif a.cmd == "download-invoices":
            from .invoices import fetcher
            fetcher.set_account(a.account)
            print(f"Amazon account: {fetcher.ACCOUNT}")
            if a.order:
                ids = a.order
            elif a.card or a.date_from or a.date_to:
                where, args = ["1=1"], []
                if a.card:
                    where.append("s.card_last4 = ?"); args.append(a.card)
                if a.date_from:
                    where.append("s.shipped_date >= ?"); args.append(a.date_from)
                if a.date_to:
                    where.append("s.shipped_date <= ?"); args.append(a.date_to)
                ids = [r[0] for r in conn.execute(f"SELECT DISTINCT s.order_id FROM shipments s WHERE {' AND '.join(where)} ORDER BY s.order_id", args)]
            elif a.all:
                ids = [r[0] for r in conn.execute("SELECT order_id FROM orders WHERE invoice_pdf IS NULL")]
            else:
                ids = [r[0] for r in conn.execute("SELECT DISTINCT m.order_id FROM matches m JOIN orders o USING(order_id) WHERE o.invoice_pdf IS NULL")]
            for oid in ids:
                db.ensure_order_stub(conn, oid, "amazon.ca")
            conn.commit()
            print(f"{len(ids)} orders to fetch")
            results = fetcher.download_many(conn, ids, headless=not a.visible)
            for oid, status in results.items():
                print(oid, status)
                fail = fetcher.INVOICE_DIR / "debug" / f"{oid}-failures.txt"
                if not status.startswith("ok") and fail.exists():
                    print("    ", fail.read_text(encoding="utf-8").splitlines()[0][:200])
            print(f"details of what was found on Amazon: {fetcher.INVOICE_DIR / 'debug'}")
            if a.out:
                import shutil
                out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
                n = 0
                for oid in ids:
                    for f in db.invoice_files(conn, oid):
                        if Path(f).exists():
                            shutil.copy(f, out / Path(f).name); n += 1
                print(f"{n} PDFs copied to {out}")
        elif a.cmd == "month-end":
            import glob, os
            from . import monthly
            from .sources import amazon_report
            from datetime import date as _date
            # 1. newest shipment-level report in Downloads (or the one given)
            report = a.report
            if not report:
                downloads = Path(os.path.expanduser("~")) / "Downloads"
                cands = sorted(glob.glob(str(downloads / "reconciliation*by*shipment*level*.csv")), key=os.path.getmtime, reverse=True)
                report = cands[0] if cands else None
            if report:
                content = Path(report).read_bytes()
                stats, errors = amazon_report.import_shipment_report(conn, content)
                db.kv_set(conn, "last_report_import", _date.today().isoformat()); conn.commit()
                print(f"1/4 imported {Path(report).name}: {stats.summary()}" + (f", {len(errors)} rows skipped" if errors else ""))
            else:
                print("1/4 no 'reconciliation by shipment level' CSV found in Downloads; skipping import (download it from Business Analytics first)")
            # 2. fetch missing invoices
            if not a.no_fetch:
                from .invoices import fetcher
                ids = [r[0] for r in conn.execute("SELECT order_id FROM orders WHERE order_id NOT IN (SELECT DISTINCT order_id FROM invoice_files)")]
                if ids:
                    results = fetcher.download_many(conn, ids, headless=True)
                    ok = sum(1 for v in results.values() if v.startswith("ok"))
                    bad = {k: v for k, v in results.items() if not v.startswith("ok")}
                    print(f"2/4 invoices fetched for {ok} of {len(ids)} orders" + (f"; problems: {bad}" if bad else ""))
                    if any("signed-out" in v for v in bad.values()):
                        print("    Amazon sign-in expired: run  python -m app.cli amazon-login  then  python -m app.cli month-end  again.")
                else:
                    print("2/4 all orders already have invoices")
            # 3. sync to the website
            if not a.no_sync:
                from .cloud import sync
                st = sync.upload(conn, log=lambda *x: None)
                print(f"3/4 website updated: {st['pdfs_uploaded']} new PDFs, {st['orders']} orders, {st['charges']} charges")
            # 4. monthly emails
            pk = monthly.run(conn, month=a.month, mode="send" if a.send else "draft", log=lambda *x: None)
            m = a.month or monthly.previous_month()
            for p in pk:
                print(f"4/4 card {p.card}: {len(p.charges)} charges, {p.pdf_count} PDFs, net {p.total:,.2f}" + (f", {len(p.missing_orders)} without invoice" if p.missing_orders else "") + (" - emailed" if a.send else " - draft written"))
            if not pk:
                print(f"4/4 no charges for {m}")
            print(f"done. {'Emails sent.' if a.send else 'Drafts are in ' + str(config.DATA_DIR / 'monthly' / m)}")
        elif a.cmd == "monthly":
            from . import monthly
            mode = "send" if a.send else ("draft" if a.draft else "files")
            monthly.run(conn, month=a.month, cards=a.card, mode=mode, out_dir=Path(a.out) if a.out else None)
        elif a.cmd == "sync":
            from .cloud import sync
            print(sync.upload(conn, dry_run=a.dry_run))
        elif a.cmd == "package":
            data, stats = export.build_package(conn, account=a.account, date_from=a.date_from, date_to=a.date_to)
            Path(a.out).write_bytes(data)
            print(f"wrote {a.out}: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
