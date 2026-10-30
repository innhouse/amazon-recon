# Amazon invoice reconciliation

A small local web app that answers the recurring question from accounting:
**"Which Amazon invoices go with these charges on card #XXXX?"**

It collects Amazon's own record of what was charged to which card (per order),
takes the charges accounting is asking about, matches the two, and produces a
zip with one PDF per charge plus a CSV, ready to send back.

## What it reads

| Source | Covers | How exact |
|---|---|---|
| **Amazon Business reconciliation report** (CSV) | Business account | Exact: every line has the card's last 4 digits, amount, date and order number |
| **"Your Transactions" page, copied as text** | Personal account | Exact: same fields, pasted from the page (personal accounts have no download) |
| **Amazon emails** over IMAP (iCloud, Outlook, Fastmail...), Gmail OAuth, or `.eml` files | Both | Adds item descriptions and the invoice link; shipment emails give the per-box charge amount |
| **Bank side**: pasted list from accounting, or the card statement CSV | RBC Visa etc. | Date + amount (+ card from the account label) |

Amazon charges the card **per shipment**, not per order, so one order can be a
dozen statement lines. The matcher works at shipment level, falls back to the
order total, and can combine several shipments into one charge.

## Quick start

```bash
cd amazon-recon
python3 -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

Typical monthly run:

1. **Amazon Business → Business Analytics → Reconciliation → Download CSV**, import it on the dashboard.
2. Personal account: open *Your Account → Your Payments → Transactions*, select all, copy, paste into the dashboard box.
3. (Optional) sync the mailbox that receives the Amazon emails, for item names and invoice links.
4. **Paste charges**: paste accounting's list (dates, amounts if given), label it e.g. `RBC Visa 3874`. The card number in the label is used to filter.
5. Open **Review** for anything not matched automatically; accept a candidate or pick manually.
6. **Download zip** for that account and date range. It contains `reconciliation.csv` and `invoices/<date>_<amount>_<order>.pdf`.

The **Charges** page also answers the question directly without any bank data:
filter by card and date range and download the CSV of order numbers.

## Official invoice PDFs (automatic)

Amazon issues one invoice per shipment, so a 16-box order has 16 invoices, each
matching one card charge. The fetcher opens Your Orders for each order, opens
its **Invoice** menu and saves every "Invoice N" PDF. One-time setup:

```powershell
pip install playwright
playwright install chromium
python -m app.cli amazon-login      # a browser opens: sign in to the account that placed the orders, then close the window
```

Then, for a request like "invoices for card 3874, Oct 7 to Oct 17":

```powershell
python -m app.cli download-invoices --card 3874 --from 2026-10-07 --to 2026-10-17 --out "$HOME\Desktop\wendy"
```

The PDFs are copied to that folder (and kept under `data/invoices`); attach
the folder's contents to the reply. Add `--visible` to watch the browser work.
The login is kept in `data/browser-profile`, like a Chrome profile; no Amazon
password is stored by the app. If Amazon asks to sign in again or shows a
captcha, run `amazon-login` again.

## Mailbox setup

**IMAP (iCloud and most others).** On the dashboard enter host, email and an
app-specific password, or set environment variables so the fields can stay empty:

```bash
export IMAP_HOST=imap.mail.me.com IMAP_USER=you@icloud.com IMAP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

iCloud requires an app-specific password from <https://account.apple.com> →
Sign-In and Security → App-Specific Passwords. The normal Apple ID password is
rejected by IMAP.

**Gmail (OAuth).** Create an OAuth client of type *Desktop app* in Google Cloud
Console with the Gmail API enabled, download it as `data/credentials.json`, then
press *Sync Gmail*. A browser window asks for read-only consent once; the token
is kept in `data/token.json`.

**.eml files.** Drag messages out of Apple Mail / Outlook and upload them.

## Command line

```
python -m app.cli sync-imap
python -m app.cli import-bank statement.csv --account "RBC Visa 3874"
python -m app.cli match
python -m app.cli package out.zip --account "RBC Visa 3874" --from 2026-10-01 --to 2026-10-31
```

## Configuration

Environment variables (all optional): `RECON_DATA_DIR`, `RECON_DB_PATH`,
`RECON_WINDOW_BEFORE` / `RECON_WINDOW_AFTER` (days around the ship date a charge
may post, default 3 / 10), `RECON_AMOUNT_TOLERANCE` (default 0.00),
`RECON_AMAZON_REGEX` (which bank descriptions count as Amazon), `RECON_GMAIL_QUERY`.

## Layout

```
app/
  main.py                 web UI (FastAPI + Jinja2)
  cli.py                  command-line entry points
  db.py                   SQLite schema and storage helpers
  parsers/amazon_email.py parses Ordered / Shipped / Refund emails
  sources/                amazon_report (Business CSV), amazon_transactions (pasted page),
                          imap_source, gmail_source, eml_source
  bank/                   statement CSV importer, quick paste parser
  recon/matcher.py        matching engine
  invoices/               official PDF fetcher (Playwright), generated summary PDF
  export.py               reconciliation CSV and zip package
tests/                    pytest suite with synthetic fixtures
data/                     SQLite DB, downloaded PDFs, browser profile (not for git)
```

Run the tests with `python -m pytest`.

## About the Amazon APIs

* **Amazon Business** has an official API with a Reconciliation API
  (`getTransactions`, per-card charges with order ids) and invoice document
  download. It requires registering as a developer on Amazon's Solution
  Provider Portal; approval takes 1–5 weeks. The reconciliation report CSV this
  app imports is the same data, downloaded from the console, so the API is an
  optional later upgrade (a `sources/amazon_business_api.py` would slot in next
  to the CSV importer).
* **Personal accounts have no API** and the old Order History Reports export
  was removed in 2023. The pasted Transactions page, the emails, and the
  browser-based invoice download are the available routes.

## Monthly routine

1. Amazon Business -> Business Analytics -> Reports -> Reconciliation by shipment level -> previous month -> Generate -> download (lands in Downloads).
2. One command does the rest (import, fetch, sync to the website, write the emails):

```powershell
python -m app.cli month-end           # drafts in data\monthly\<month>\
python -m app.cli month-end --send    # emails them to accounting
```

If it reports the Amazon sign-in expired: `python -m app.cli amazon-login`, then run month-end again.
The individual steps remain available: `import-report`, `download-invoices --all`, `sync`, `monthly`.

## Monthly package to accounting

`python -m app.cli monthly` builds, for the previous month and for each card, a zip with the
month's invoice PDFs plus a CSV of the statement lines (date, "AMAZON* code", amount, order,
item), and can email one message per card.

```powershell
# once: who receives it and how it is sent (a Google app password for the sending address)
setx RECON_MAIL_TO "wendy@innhouse.bc.ca"
setx RECON_MAIL_CC "amin@innhouse.bc.ca"
setx RECON_SMTP_USER "amin@innhouse.bc.ca"
setx RECON_SMTP_PASSWORD "xxxx xxxx xxxx xxxx"

python -m app.cli monthly --draft          # writes zips + .eml drafts to data\monthly\<month>\ for review
python -m app.cli monthly --send           # emails them
python -m app.cli monthly --month 2026-09 --card 3874 --send
```

Orders whose invoice is not stored yet are listed in the email as "will follow" rather than
skipped. Run `download-invoices --all` first to minimise those.

Schedule it on the PC (3rd of each month at 9:00; the report import and fetch must have run first):

```powershell
schtasks /Create /SC MONTHLY /D 3 /ST 09:00 /TN "Amazon invoices monthly" /TR "powershell -NoProfile -Command \"cd $HOME\amazon-recon; .\.venv\Scripts\python.exe -m app.cli monthly --send\""
```
