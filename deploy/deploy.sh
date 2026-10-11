#!/usr/bin/env bash
# Deploys the Google Ads MCP server to Cloud Run.
#
# Run this yourself, after `gcloud auth login`: it needs your Google Cloud
# identity, a billing-enabled project and the OAuth client you created.
# Nothing here prints a secret. Read deploy/README.md first.
#
# Usage:
#   deploy/deploy.sh                 # build + deploy the current checkout
#   PROJECT=md-kampus REGION=europe-west1 deploy/deploy.sh
set -euo pipefail

PROJECT="${PROJECT:-md-kampus}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-google-ads-mcp}"
REPO="${REPO:-mcp-servers}"                 # Artifact Registry repository
BUCKET="${BUCKET:-${PROJECT}-google-ads-mcp}" # plans, audit log, quota, limits
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-google-ads-mcp@${PROJECT}.iam.gserviceaccount.com}"
ALLOWED_EMAILS="${ALLOWED_EMAILS:?Set ALLOWED_EMAILS=you@example.com,teammate@example.com}"
LOGIN_CUSTOMER_ID="${LOGIN_CUSTOMER_ID:?Set LOGIN_CUSTOMER_ID to the MCC id (digits only)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:$(git rev-parse --short HEAD)"

echo ">> project=${PROJECT} region=${REGION} service=${SERVICE}"
echo ">> image=${IMAGE}"
gcloud config set project "${PROJECT}" >/dev/null

# --- one-time pieces: safe to re-run --------------------------------------
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com storage.googleapis.com >/dev/null

gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${REPO}" --repository-format=docker --location="${REGION}"

gcloud iam service-accounts describe "${SERVICE_ACCOUNT}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${SERVICE_ACCOUNT%%@*}" --display-name="Google Ads MCP"

gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" --uniform-bucket-level-access

# Least privilege: the service account reads its secrets and owns the bucket's
# objects; nothing else in the project.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" --role="roles/storage.objectAdmin" >/dev/null
for secret in google-ads-developer-token google-ads-oauth-client-id google-ads-oauth-client-secret google-ads-jwt-signing-key; do
  gcloud secrets describe "${secret}" >/dev/null 2>&1 \
    || { echo "!! secret ${secret} is missing: create it first (see deploy/README.md)"; exit 1; }
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${SERVICE_ACCOUNT}" --role="roles/secretmanager.secretAccessor" >/dev/null
done

# --- build ------------------------------------------------------------------
gcloud builds submit --tag "${IMAGE}" .

# --- deploy -----------------------------------------------------------------
# The URL is only known after the first deploy; GOOGLE_ADS_MCP_BASE_URL is set
# in a second step below so OAuth redirects match.
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --platform=managed \
  --service-account="${SERVICE_ACCOUNT}" \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=20 \
  --timeout=300 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_ADS_MCP_TRANSPORT=http,GOOGLE_ADS_MCP_ACCESS_LOG=true,GOOGLE_ADS_LOGIN_CUSTOMER_ID=${LOGIN_CUSTOMER_ID},GOOGLE_ADS_MCP_ALLOWED_EMAILS=${ALLOWED_EMAILS},GOOGLE_ADS_MCP_PLAN_GCS_BUCKET=${BUCKET},GOOGLE_ADS_MCP_AUDIT_GCS_BUCKET=${BUCKET},GOOGLE_ADS_MCP_QUOTA_GCS_BUCKET=${BUCKET},GOOGLE_ADS_MCP_LIMITS_GCS_URI=gs://${BUCKET}/limits.yaml,GOOGLE_ADS_MCP_STORAGE_TYPE=firestore,GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT=${PROJECT}" \
  --set-secrets="GOOGLE_ADS_DEVELOPER_TOKEN=google-ads-developer-token:latest,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=google-ads-oauth-client-id:latest,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=google-ads-oauth-client-secret:latest,GOOGLE_ADS_MCP_JWT_SIGNING_KEY=google-ads-jwt-signing-key:latest"

URL="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"
gcloud run services update "${SERVICE}" --region="${REGION}" \
  --update-env-vars="GOOGLE_ADS_MCP_BASE_URL=${URL}" >/dev/null

echo
echo ">> deployed: ${URL}/mcp"
echo ">> add ${URL}/auth/callback to the OAuth client's authorized redirect URIs"
echo ">> --allow-unauthenticated is Cloud Run's network layer only: the server"
echo ">> itself refuses every caller whose Google account is not on the allow list."
echo ">> previous revisions: deploy/rollback.sh"
