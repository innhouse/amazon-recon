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

from . import db, export
from .bank import importer
from .recon import matcher


def main(argv=None):
    p = argparse.ArgumentParser(prog="amazon-recon")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync-imap"); s.add_argument("--lookback", type=int, default=400)
    sub.add_parser("sync-gmail")
    s = sub.add_parser("import-eml"); s.add_argument("folder")
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
