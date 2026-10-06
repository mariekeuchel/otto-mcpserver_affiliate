#!/usr/bin/env bash
# Setup + Deployment des OTTO-Affiliate-MCP-Servers auf Google Cloud Run.
#
# Voraussetzungen: gcloud CLI, eingeloggt (gcloud auth login), Projekt mit Billing.
# Aufruf:
#   ./deploy/deploy.sh                      # Projekt dmx-data-248209
#   PROJECT_ID=anderes-projekt ./deploy/deploy.sh
#
# Die OTTO-Zugangsdaten müssen bereits im Secret Manager liegen:
#   otto_api_access_token  -> OTTO_API_ACCESS_TOKEN
#   otto_publisher_id      -> OTTO_PUBLISHER_ID
# Das Secret für MCP_AUTH_TOKEN legt das Skript beim ersten Lauf an (Wert wird generiert,
# sofern MCP_AUTH_TOKEN nicht als Umgebungsvariable übergeben wird).
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-dmx-data-248209}"
REGION="${REGION:-europe-west3}"            # Frankfurt
SERVICE="${SERVICE:-otto-affiliate-mcp}"
REPO="${REPO:-services}"
SA_NAME="${SA_NAME:-otto-affiliate-mcp}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

SECRET_OTTO_TOKEN="${SECRET_OTTO_TOKEN:-otto_api_access_token}"
SECRET_OTTO_PUBLISHER="${SECRET_OTTO_PUBLISHER:-otto_publisher_id}"
SECRET_MCP_TOKEN="${SECRET_MCP_TOKEN:-otto-affiliate-mcp-auth-token}"

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

echo "==> Service Account (eigener Account nur für diesen Dienst, keine Projekt-Rollen)"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="OTTO Affiliate MCP (Cloud Run)" \
    --description="Laufzeit-Identität des Cloud-Run-Dienstes ${SERVICE}; liest nur seine Secrets."

has_version() { gcloud secrets versions list "$1" --filter="state=ENABLED" --limit=1 --format='value(name)' 2>/dev/null | grep -q .; }

grant_access() {
  # Zugriff pro Secret statt projektweit (Least Privilege)
  gcloud secrets add-iam-policy-binding "$1" \
    --member="serviceAccount:${SA_EMAIL}" --role=roles/secretmanager.secretAccessor >/dev/null
}

echo "==> Secrets"
for secret in "$SECRET_OTTO_TOKEN" "$SECRET_OTTO_PUBLISHER"; do
  if ! has_version "$secret"; then
    echo "Fehler: Secret '${secret}' fehlt oder hat keine aktive Version im Projekt ${PROJECT_ID}." >&2
    exit 1
  fi
done

if ! gcloud secrets describe "$SECRET_MCP_TOKEN" >/dev/null 2>&1; then
  gcloud secrets create "$SECRET_MCP_TOKEN" --replication-policy=automatic
fi
if [[ -n "${MCP_AUTH_TOKEN:-}" ]] || ! has_version "$SECRET_MCP_TOKEN"; then
  if [[ -z "${MCP_AUTH_TOKEN:-}" ]]; then
    MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
    echo "Generiertes MCP_AUTH_TOKEN (sicher aufbewahren!): ${MCP_AUTH_TOKEN}"
  fi
  printf '%s' "$MCP_AUTH_TOKEN" | gcloud secrets versions add "$SECRET_MCP_TOKEN" --data-file=-
fi

for secret in "$SECRET_OTTO_TOKEN" "$SECRET_OTTO_PUBLISHER" "$SECRET_MCP_TOKEN"; do
  grant_access "$secret"
done

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
  --set-secrets="OTTO_API_ACCESS_TOKEN=${SECRET_OTTO_TOKEN}:latest,OTTO_PUBLISHER_ID=${SECRET_OTTO_PUBLISHER}:latest,MCP_AUTH_TOKEN=${SECRET_MCP_TOKEN}:latest" \
  --cpu=1 --memory=1Gi --min-instances=0 --max-instances=3 --timeout=300 \
  "$AUTH_FLAG"

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
echo
echo "Fertig. MCP-Endpunkt: ${URL}/mcp"
echo "Health-Check:        ${URL}/health"
