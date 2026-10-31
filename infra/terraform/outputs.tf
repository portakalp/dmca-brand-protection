output "api_url" {
  description = "Cloud Run api URL (run.app): the edge Worker's API_ORIGIN and the deploy's smoke-test target."
  value       = google_cloud_run_v2_service.api.uri
}

output "web_url" {
  description = "Cloud Run web URL (run.app): the edge Worker's WEB_ORIGIN."
  value       = google_cloud_run_v2_service.web.uri
}

output "vm_name" {
  description = "Worker VM name (GitHub variable VM_NAME)."
  value       = google_compute_instance.vm.name
}

output "evidence_bucket" {
  description = "Evidence bucket (GCS_EVIDENCE_BUCKET)."
  value       = google_storage_bucket.evidence.name
}

output "reports_bucket" {
  description = "Reports bucket (GCS_REPORTS_BUCKET)."
  value       = google_storage_bucket.reports.name
}

output "cloudsql_connection_name" {
  description = "Cloud SQL connection name (project:region:instance) for the Auth Proxy and Cloud Run."
  value       = google_sql_database_instance.main.connection_name
}

output "artifact_registry" {
  description = "Docker registry path; images are <registry>/backend:<sha>, <registry>/browser:<sha> and <registry>/web:<sha>."
  value       = google_artifact_registry_repository.kalkan.registry_uri
}

output "wif_provider" {
  description = "Full Workload Identity provider name with the project number (GitHub variable GCP_WIF_PROVIDER)."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deploy_service_account" {
  description = "Service account the deploy impersonates (GitHub variable GCP_DEPLOY_SA)."
  value       = google_service_account.kalkan["ci"].email
}

output "github_variables" {
  description = "Ready gh commands for the repository variables a deploy workflow reads (README.md, Deploying); the commented ones are yours to choose."
  value       = <<-EOT
    gh variable set GCP_PROJECT_ID --repo '${var.github_repo}' --body '${var.project_id}'
    gh variable set GCP_REGION --repo '${var.github_repo}' --body '${var.region}'
    gh variable set GCP_ZONE --repo '${var.github_repo}' --body '${var.zone}'
    gh variable set GCP_WIF_PROVIDER --repo '${var.github_repo}' --body '${google_iam_workload_identity_pool_provider.github.name}'
    gh variable set GCP_DEPLOY_SA --repo '${var.github_repo}' --body '${google_service_account.kalkan["ci"].email}'
    gh variable set VM_NAME --repo '${var.github_repo}' --body '${google_compute_instance.vm.name}'
    gh variable set APP_DOMAIN --repo '${var.github_repo}' --body '${var.domain}'
    gh variable set OPS_ALERT_EMAIL --repo '${var.github_repo}' --body '${var.ops_alert_email}'
    # Choose these yourself, then remove the leading "# ":
    # gh variable set MAIL_FROM --repo '${var.github_repo}' --body 'abuse-reports@<sending domain>'
    # gh variable set REPLY_DOMAIN --repo '${var.github_repo}' --body '<reply domain>'
    # gh variable set DRY_RUN_REDIRECT_EMAIL --repo '${var.github_repo}' --body '<your mailbox>'
    # gh variable set CASE_PREFIX --repo '${var.github_repo}' --body 'KLK'
    # Alert mail relay for Alertmanager on the VM (its password is the alert-smtp-password secret):
    # gh variable set ALERT_SMTP_SMARTHOST --repo '${var.github_repo}' --body 'smtp.example.com:587'
    # gh variable set ALERT_SMTP_USERNAME --repo '${var.github_repo}' --body '<smtp user>'
    # Optional VM compose profiles, comma-separated from browser, ai, ops (unset = none):
    # gh variable set COMPOSE_PROFILES --repo '${var.github_repo}' --body 'browser'
    # After the first deploy with the web service:
    # gh variable set WEB_ENABLED --repo '${var.github_repo}' --body 'true'
  EOT
}
