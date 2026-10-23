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
    s = sub.add_parser("download-invoices"); s.add_argument("--all", action="store_true"); s.add_argument("--visible", action="store_true")
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
            fetcher.interactive_login(a.marketplace)
        elif a.cmd == "download-invoices":
            from .invoices import fetcher
            if a.all:
                ids = [r[0] for r in conn.execute("SELECT order_id FROM orders WHERE invoice_pdf IS NULL")]
            else:
                ids = [r[0] for r in conn.execute("SELECT DISTINCT m.order_id FROM matches m JOIN orders o USING(order_id) WHERE o.invoice_pdf IS NULL")]
            for oid, status in fetcher.download_many(conn, ids, headless=not a.visible).items():
                print(oid, status)
        elif a.cmd == "package":
            data, stats = export.build_package(conn, account=a.account, date_from=a.date_from, date_to=a.date_to)
            Path(a.out).write_bytes(data)
            print(f"wrote {a.out}: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
