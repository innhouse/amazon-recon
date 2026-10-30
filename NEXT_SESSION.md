# Next session: move the app to the Firebase stack

Decision (Amin, 2026-10-24): align this app with the other InnHouse apps
(InnProp, portDash): Firebase Hosting front end, Firestore + Cloud Storage for
data, `firebase deploy` for releases. The Amazon invoice fetcher stays on
Amin's PC because the Amazon sign-in must live on a machine he controls.

## Current state (keep working until the replacement is live)
- Python/FastAPI app in `app/`, SQLite DB, runs on Amin's PC (`uvicorn app.main:app --reload`).
- Cloud Run service `amazon-recon` (project innprop-innhouse, northamerica-northeast1),
  behind IAP (Amin allowed), reading a bucket mount `gs://innprop-amazon-recon-data`
  at /data (read-only preview). Continuous deployment from GitHub main via Cloud Build.
- Data on the PC: `data/recon.sqlite3`, `data/invoices/*.pdf` (199 PDFs, 118 orders, Jan-Oct 2026
  Business account). Browser profile(s) in `data/browser-profile*` (never upload).

## Built on 2026-10-24 (not yet deployed)
- `firebase/`: Hosting site config (target amazon-recon), isolated Firestore rules for the named
  database `amazon-recon`, Storage rules for bucket innprop-amazon-recon-data, composite indexes,
  and the static web app (`firebase/web`: Google sign-in limited to innhouse.bc.ca, data tiles,
  filter by card/dates, paste statement lines, zip of invoices built in the browser, CSV).
- `app/cloud/sync.py` + `python -m app.cli sync`: uploads orders/charges/refunds to Firestore and
  PDFs to the bucket (idempotent). Tested payload builder.
- Deploy steps: `firebase/README.md` (one-time: create the named database, link the bucket in
  Firebase Storage, create the hosting site, enable Google sign-in; then `firebase deploy`).

- `python -m app.cli monthly`: per-card monthly zip + CSV, emailed (SMTP app password) or
  drafted as .eml; schedule with Task Scheduler (README). Fully automatic month-end still needs
  the Amazon report download automated (Business API application, or Playwright on the
  Business Analytics report page) and the fetch run before it.

## Target (remaining)
1. Deploy what is built (see firebase/README.md) and verify sign-in, filter, zip with real data.
2. Decide whether report import stays on the PC (current design: import on PC, then `sync`)
   or moves to a Cloud Function; the PC design needs no server code.
3. Decommission the Cloud Run service + bucket mount once the Firebase site is in use
   (keep the bucket: it holds the PDFs).
4. A "requests" log (what was sent to accounting and when) in Firestore.
5. Custom domain (e.g. invoices.innhouse.bc.ca) under Hosting.
6. Personal accounts: Transactions-page paste as a first-class import; per-account sign-ins
   (business / personal / fillah). Wendy's August 2026 list was all personal-side on card 3874.
7. Nice-to-have: read totals from each PDF to tag it with its statement code; apply for the
   Amazon Business API (Solution Provider Portal) to automate report download.

## Known small bugs to carry over
- Hosted home page counts "(0 PDFs)" when paths are resolved under a mounted folder
  (use `db._resolve_data_path` in the count).
- GitHub repository is public; should be private.
