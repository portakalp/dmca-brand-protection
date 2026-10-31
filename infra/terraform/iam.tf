# Service accounts and least-privilege roles (§22, §24). No service account keys: GitHub Actions
# impersonates kalkan-ci through Workload Identity Federation (wif.tf). Secret access is granted
# per secret in secrets.tf.
#
#   kalkan-api  Cloud SQL client; reads both buckets; its secrets
#   kalkan-web  its secret (EDGE_SHARED_SECRET); runs the Cloud Run web service
#   kalkan-vm   Cloud SQL client; writes evidence (no delete, retention), manages reports;
#               pulls images; writes logs and metrics; its secrets. Web Risk uses an API key.
#   kalkan-ci   pushes images, deploys Cloud Run as api/web, SSHes to the VM through IAP with
#               OS Login (sudo), lists versions of the secrets Cloud Run binds

locals {
  # K77: §22's sa-vm and sa-ci are below GCP's 6-character minimum.
  service_accounts = {
    api = "Cloud Run api"
    web = "Cloud Run web"
    vm  = "Worker VM kalkan-vm"
    ci  = "GitHub Actions deploy through Workload Identity Federation"
  }

  project_roles = {
    "api-cloudsql-client" = { sa = "api", role = "roles/cloudsql.client" }
    "vm-cloudsql-client"  = { sa = "vm", role = "roles/cloudsql.client" }
    "vm-log-writer"       = { sa = "vm", role = "roles/logging.logWriter" }
    "vm-metric-writer"    = { sa = "vm", role = "roles/monitoring.metricWriter" }
    # Project level: covers api and web. It cannot change IAM policies.
    "ci-run-developer" = { sa = "ci", role = "roles/run.developer" }
    # gcloud compute ssh reads the instance and the project (compute.instances.get, projects.get).
    "ci-compute-viewer" = { sa = "ci", role = "roles/compute.viewer" }
  }

  bucket_roles = {
    "evidence-vm-create" = { bucket = google_storage_bucket.evidence.name, sa = "vm", role = "roles/storage.objectCreator" }
    "evidence-vm-read"   = { bucket = google_storage_bucket.evidence.name, sa = "vm", role = "roles/storage.objectViewer" }
    "reports-vm-admin"   = { bucket = google_storage_bucket.reports.name, sa = "vm", role = "roles/storage.objectAdmin" }
    "evidence-api-read"  = { bucket = google_storage_bucket.evidence.name, sa = "api", role = "roles/storage.objectViewer" }
    "reports-api-read"   = { bucket = google_storage_bucket.reports.name, sa = "api", role = "roles/storage.objectViewer" }
  }
}

resource "google_service_account" "kalkan" {
  for_each = local.service_accounts

  account_id   = "kalkan-${each.key}"
  display_name = "kalkan-${each.key}"
  description  = each.value
}

resource "google_project_iam_member" "kalkan" {
  for_each = local.project_roles

  project = var.project_id
  role    = each.value.role
  member  = google_service_account.kalkan[each.value.sa].member
}

resource "google_storage_bucket_iam_member" "kalkan" {
  for_each = local.bucket_roles

  bucket = each.value.bucket
  role   = each.value.role
  member = google_service_account.kalkan[each.value.sa].member
}

resource "google_artifact_registry_repository_iam_member" "ci_writer" {
  location   = google_artifact_registry_repository.kalkan.location
  repository = google_artifact_registry_repository.kalkan.name
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.kalkan["ci"].member
}

resource "google_artifact_registry_repository_iam_member" "vm_reader" {
  location   = google_artifact_registry_repository.kalkan.location
  repository = google_artifact_registry_repository.kalkan.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.kalkan["vm"].member
}

# kalkan-ci acts as the runtime accounts: Cloud Run revisions run as api/web, and OS Login on a
# VM with an attached service account requires actAs on it.
resource "google_service_account_iam_member" "ci_act_as" {
  for_each = toset(["api", "web", "vm"])

  service_account_id = google_service_account.kalkan[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.kalkan["ci"].member
}

# IAP TCP forwarding and OS Login with sudo (docker compose) on this VM only.
resource "google_iap_tunnel_instance_iam_member" "ci_tunnel" {
  zone     = google_compute_instance.vm.zone
  instance = google_compute_instance.vm.name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = google_service_account.kalkan["ci"].member
}

resource "google_compute_instance_iam_member" "ci_os_admin_login" {
  zone          = google_compute_instance.vm.zone
  instance_name = google_compute_instance.vm.name
  role          = "roles/compute.osAdminLogin"
  member        = google_service_account.kalkan["ci"].member
}
