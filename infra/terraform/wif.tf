# Workload Identity Federation for GitHub Actions (§22; K76). Deploys run only from the first
# attempt of a manual workflow_dispatch of the deploy workflow (var.github_deploy_workflow) on
# main, started by the deployer; Google rejects every other token. The repository ships no deploy
# workflow: README.md ("Deploying") lists the steps one has to run. Numeric ids: the repository uses GitHub's immutable OIDC subject
# format, and names can be re-registered.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "Deploys from ${var.github_repo}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions deploy"

  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.actor_id"      = "assertion.actor_id"
    "attribute.event_name"    = "assertion.event_name"
    "attribute.ref"           = "assertion.ref"
    "attribute.workflow_ref"  = "assertion.workflow_ref"
  }

  # workflow_ref is compared exactly: with ref pinned to main it is the workflow path plus "@" and
  # refs/heads/main, and plain equality needs no CEL string functions. A re-run keeps the first
  # attempt's actor_id whoever presses "Re-run", so only attempt 1 is trusted: redeploy with a new
  # workflow_dispatch instead.
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.actor_id == '${var.github_deployer_id}'",
    "assertion.event_name == 'workflow_dispatch'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.workflow_ref == '${var.github_repo}/${var.github_deploy_workflow}@refs/heads/main'",
    "assertion.run_attempt == '1'",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "ci_wif" {
  service_account_id = google_service_account.kalkan["ci"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}
