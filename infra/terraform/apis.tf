# §22's APIs plus sts, cloudresourcemanager and serviceusage (WIF and Terraform) and oslogin
# (gcloud compute ssh with OS Login); K77. bootstrap.sh enables the same list before the
# first apply because activation is eventually consistent: keep the two lists in sync.
locals {
  services = toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "iap.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "oslogin.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
    "webrisk.googleapis.com",
  ])
}

resource "google_project_service" "kalkan" {
  for_each = local.services

  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}
