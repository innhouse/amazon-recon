# Amazon invoices on Firebase

Front end and rules for the hosted version of the app. Deploy from this folder on a PC where
the Firebase CLI is signed in (`firebase login`). One-time setup, then `firebase deploy`.

## One-time setup (project innprop-innhouse)

```powershell
cd "$HOME\amazon-recon\firebase"
firebase use innprop-innhouse

# 1. A separate Firestore database for this app (its rules never touch InnProp's).
gcloud firestore databases create --database=amazon-recon --location=northamerica-northeast1 --project innprop-innhouse

# 2. Link the invoice bucket to Firebase Storage so its rules can be deployed:
#    Firebase console -> Storage -> "Add bucket" -> Import existing bucket -> innprop-amazon-recon-data

# 3. A Hosting site for this app, next to InnProp's.
firebase hosting:sites:create amazon-recon-innhouse
firebase target:apply hosting amazon-recon amazon-recon-innhouse

# 4. Google sign-in: Firebase console -> Authentication -> Sign-in method -> Google -> Enable.
#    Add the hosting domain under Authentication -> Settings -> Authorized domains if it is not listed.

# 5. Deploy rules, indexes and the site.
firebase deploy --only "firestore,storage,hosting:amazon-recon"
```

Site: https://amazon-recon-innhouse.web.app (a custom domain such as invoices.innhouse.bc.ca can be
added under Hosting -> Add custom domain).

## Loading data

On the PC that holds the invoices:

```powershell
cd "$HOME\amazon-recon"
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
gcloud auth application-default login      # once
python -m app.cli sync                      # after every import / fetch
```

`sync` writes orders, charges and refunds to the `amazon-recon` Firestore database and uploads
new PDFs to gs://innprop-amazon-recon-data/invoices/<order>/. It is safe to re-run.

## Who can see it

Anyone who signs in with a Google account ending in @innhouse.bc.ca (enforced by the rules in
`firestore.rules` and `storage.rules`). Nobody can write from the web; only the PC sync writes.
