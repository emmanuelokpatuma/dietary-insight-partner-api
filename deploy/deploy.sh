#!/usr/bin/env bash
# Deploy the partner API to Cloud Run in London (europe-west2).
#
#   PROJECT_ID=my-project ./deploy/deploy.sh
#
# Safe to re-run: it creates what is missing and redeploys the code.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your Google Cloud project id}"
REGION="${REGION:-europe-west2}"
SERVICE="${SERVICE:-dietary-insight-partner-api}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"        # cost ceiling; raise as traffic grows
DAILY_AI_CALL_CAP="${DAILY_AI_CALL_CAP:-500}"
RUNTIME_SA_NAME="partner-api-runtime"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

cd "$(dirname "$0")/.."
gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

echo "==> Runtime service account (least privilege)"
if ! gcloud iam service-accounts describe "$RUNTIME_SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" --display-name="Partner API runtime"
fi

echo "==> Secrets (stored in $REGION)"
for s in MONGO_URI GEMINI_API_KEY; do
  if ! gcloud secrets describe "$s" >/dev/null 2>&1; then
    read -r -s -p "Paste value for $s (input hidden), then Enter: " value; echo
    printf '%s' "$value" | gcloud secrets create "$s" --replication-policy=user-managed \
      --locations="$REGION" --data-file=-
    unset value
  fi
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
done

echo "==> Building and deploying $SERVICE"
# --allow-unauthenticated: partners authenticate with API keys inside the app, not with Google IAM.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances "$MAX_INSTANCES" \
  --concurrency 40 \
  --cpu 1 --memory 512Mi \
  --timeout 90 \
  --set-env-vars "ENV=production,DAILY_AI_CALL_CAP=${DAILY_AI_CALL_CAP}" \
  --set-secrets "MONGO_URI=MONGO_URI:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "Deployed: $URL"
echo "Health check: curl $URL/healthz"
