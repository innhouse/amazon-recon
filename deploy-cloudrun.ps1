# Deploys the web app to Cloud Run in your Google Cloud project, reachable only by signed-in Google users you allow.
# Run from the amazon-recon folder on a PC where gcloud is signed in:  .\deploy-cloudrun.ps1
$project = "innprop-innhouse"
$region  = "northamerica-northeast1"
gcloud run deploy amazon-recon --source . --project $project --region $region --no-allow-unauthenticated --memory 512Mi
# Then grant access to the people who may open it (repeat per person):
#   gcloud run services add-iam-policy-binding amazon-recon --project $project --region $region --member="user:amin@innhouse.bc.ca" --role="roles/run.invoker"
# Note: Cloud Run's disk is temporary. For a durable database set RECON_DB_PATH to a mounted Cloud Storage bucket or move to Cloud SQL.
