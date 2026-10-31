#!/usr/bin/env bash
# One-off setup before the first `terraform init` (infra/terraform/README.md): enables
# the project's APIs and creates the Terraform state bucket. Safe to run again.
#
#   PROJECT_ID=my-gcp-project infra/terraform/bootstrap.sh
#   PROJECT_ID=my-gcp-project REGION=europe-central2 infra/terraform/bootstrap.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID, e.g. PROJECT_ID=my-gcp-project}"
REGION="${REGION:-europe-central2}"
BUCKET="gs://${PROJECT_ID}-tfstate"

# Terraform needs these before it can manage anything else.
TERRAFORM_APIS=(
  serviceusage.googleapis.com
  cloudresourcemanager.googleapis.com
  storage.googleapis.com
  iam.googleapis.com
)

# Same list as apis.tf; enabling early avoids "API has not been used" errors during the first
# apply, because activation is eventually consistent.
PROJECT_APIS=(
  artifactregistry.googleapis.com
  cloudresourcemanager.googleapis.com
  compute.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  iap.googleapis.com
  logging.googleapis.com
  monitoring.googleapis.com
  oslogin.googleapis.com
  run.googleapis.com
  secretmanager.googleapis.com
  serviceusage.googleapis.com
  sqladmin.googleapis.com
  storage.googleapis.com
  sts.googleapis.com
  webrisk.googleapis.com
)

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is not installed: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi

account="$(gcloud config get-value account 2>/dev/null || true)"
echo "Project ${PROJECT_ID}, region ${REGION}, gcloud account ${account:-<none>}"
gcloud projects describe "${PROJECT_ID}" --format="value(projectId)" >/dev/null

echo "Enabling the APIs Terraform needs..."
gcloud services enable "${TERRAFORM_APIS[@]}" --project "${PROJECT_ID}"
echo "Enabling the project's APIs..."
gcloud services enable "${PROJECT_APIS[@]}" --project "${PROJECT_ID}"

if gcloud storage buckets describe "${BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "State bucket ${BUCKET} already exists."
else
  echo "Creating state bucket ${BUCKET}..."
  if ! gcloud storage buckets create "${BUCKET}" --project "${PROJECT_ID}" \
    --location "${REGION}" --uniform-bucket-level-access --public-access-prevention; then
    echo "Could not create ${BUCKET}. Bucket names are global; is the name taken?" >&2
    exit 1
  fi
fi

# Versioning keeps every earlier state; uniform access and public access prevention keep it private.
gcloud storage buckets update "${BUCKET}" --project "${PROJECT_ID}" \
  --versioning --uniform-bucket-level-access --public-access-prevention

cat <<EOF

Done. Next steps (infra/terraform/README.md):
  cd infra/terraform
  cp terraform.tfvars.example terraform.tfvars    # set project_id, domain, ops_alert_email, github_*
  gcloud auth application-default login           # credentials Terraform uses
  terraform init -backend-config="bucket=${PROJECT_ID}-tfstate"
  terraform plan -out=tfplan                      # read it before applying
  terraform apply tfplan
EOF
