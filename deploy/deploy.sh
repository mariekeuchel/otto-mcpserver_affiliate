#!/usr/bin/env bash
# Einmaliges Setup + Deployment des OTTO-Affiliate-MCP-Servers auf Google Cloud Run.
#
# Voraussetzungen: gcloud CLI, eingeloggt (gcloud auth login), Projekt mit Billing.
# Aufruf:
#   PROJECT_ID=mein-projekt ./deploy/deploy.sh
#
# Secrets werden beim ersten Lauf abgefragt (oder per Umgebungsvariable übergeben):
#   OTTO_API_ACCESS_TOKEN, OTTO_PUBLISHER_ID, MCP_AUTH_TOKEN (wird sonst generiert)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?PROJECT_ID setzen}"
REGION="${REGION:-europe-west3}"            # Frankfurt
SERVICE="${SERVICE:-otto-affiliate-mcp}"
REPO="${REPO:-mcp}"
SA_NAME="${SA_NAME:-otto-affiliate-mcp}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
# "public": Zugriff nur per Bearer-Token (für Claude.ai & Co.)
# "iam":    zusätzlich Cloud-Run-IAM (nur Google-Identitäten mit roles/run.invoker)
ACCESS_MODE="${ACCESS_MODE:-public}"

cd "$(dirname "$0")/.."
gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> APIs aktivieren"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

echo "==> Artifact Registry"
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION"

echo "==> Service Account"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="OTTO Affiliate MCP"

upsert_secret() {
  local name="$1" value="$2"
  if ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=automatic
  fi
  if [[ -n "$value" ]]; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=-
  fi
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:${SA_EMAIL}" --role=roles/secretmanager.secretAccessor >/dev/null
}

has_version() { gcloud secrets versions list "$1" --limit=1 --format='value(name)' 2>/dev/null | grep -q .; }

echo "==> Secrets"
if ! has_version otto-api-access-token 2>/dev/null && [[ -z "${OTTO_API_ACCESS_TOKEN:-}" ]]; then
  read -rsp "OTTO API Access-Token: " OTTO_API_ACCESS_TOKEN; echo
fi
if ! has_version otto-publisher-id 2>/dev/null && [[ -z "${OTTO_PUBLISHER_ID:-}" ]]; then
  read -rp "OTTO Publisher-ID: " OTTO_PUBLISHER_ID
fi
if ! has_version mcp-auth-token 2>/dev/null && [[ -z "${MCP_AUTH_TOKEN:-}" ]]; then
  MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
  echo "Generiertes MCP_AUTH_TOKEN (sicher aufbewahren!): ${MCP_AUTH_TOKEN}"
fi
upsert_secret otto-api-access-token "${OTTO_API_ACCESS_TOKEN:-}"
upsert_secret otto-publisher-id "${OTTO_PUBLISHER_ID:-}"
upsert_secret mcp-auth-token "${MCP_AUTH_TOKEN:-}"

echo "==> Build (Cloud Build)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(date +%Y%m%d-%H%M%S)"
gcloud builds submit --tag "$IMAGE" .

echo "==> Deploy (Cloud Run)"
AUTH_FLAG="--allow-unauthenticated"
[[ "$ACCESS_MODE" == "iam" ]] && AUTH_FLAG="--no-allow-unauthenticated"
gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --set-secrets=OTTO_API_ACCESS_TOKEN=otto-api-access-token:latest,OTTO_PUBLISHER_ID=otto-publisher-id:latest,MCP_AUTH_TOKEN=mcp-auth-token:latest \
  --cpu=1 --memory=512Mi --min-instances=0 --max-instances=3 --timeout=300 \
  "$AUTH_FLAG"

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
echo
echo "Fertig. MCP-Endpunkt: ${URL}/mcp"
echo "Health-Check:        ${URL}/healthz"
