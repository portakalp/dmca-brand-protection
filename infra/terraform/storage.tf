# Evidence and report buckets (§11, §22; K14, K44). Objects are written once and streamed
# through the API; nothing is public. Access is granted per bucket in iam.tf.

resource "google_storage_bucket" "evidence" {
  name                        = "${var.project_id}-evidence"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # Unlocked. Locking is irreversible, so it is left to the operator: this reference never sets
  # is_locked = true.
  retention_policy {
    retention_period = "15552000" # 180 days
    is_locked        = false
  }

  # Case evidence and messages go on day 365; brand assets and LOAs stay (K44).
  lifecycle_rule {
    condition {
      age            = 365
      matches_prefix = ["cases/", "messages/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket" "reports" {
  name                        = "${var.project_id}-reports"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle {
    prevent_destroy = true
  }
}
