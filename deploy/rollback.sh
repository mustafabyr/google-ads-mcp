#!/usr/bin/env bash
# Sends all traffic back to the previous Cloud Run revision (or a named one).
#
# Usage:
#   deploy/rollback.sh                    # previous revision
#   deploy/rollback.sh google-ads-mcp-00012-abc
set -euo pipefail

PROJECT="${PROJECT:-md-kampus}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-google-ads-mcp}"
TARGET="${1:-}"

gcloud config set project "${PROJECT}" >/dev/null

if [[ -z "${TARGET}" ]]; then
  # Revisions newest first; the second one is the previous deploy.
  TARGET="$(gcloud run revisions list --service="${SERVICE}" --region="${REGION}" \
    --sort-by='~metadata.creationTimestamp' --format='value(metadata.name)' | sed -n '2p')"
  [[ -n "${TARGET}" ]] || { echo "!! no previous revision to roll back to"; exit 1; }
fi

echo ">> routing 100% of traffic to ${TARGET}"
gcloud run services update-traffic "${SERVICE}" --region="${REGION}" --to-revisions="${TARGET}=100"
gcloud run services describe "${SERVICE}" --region="${REGION}" \
  --format='table(status.traffic.revisionName,status.traffic.percent)'
