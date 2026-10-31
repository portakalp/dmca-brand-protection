# Cloud SQL for PostgreSQL 16 (§22; K77). Public IP without authorized networks: only the
# Cloud SQL Auth Proxy (VM) and the Cloud Run connector (api) can connect.

resource "google_sql_database_instance" "main" {
  name                = "kalkan-db"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = true

  settings {
    # PostgreSQL 16 defaults to ENTERPRISE_PLUS, which rejects shared-core tiers.
    edition                     = "ENTERPRISE"
    tier                        = "db-f1-micro"
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = true
    deletion_protection_enabled = true
    retain_backups_on_delete    = true
    connector_enforcement       = "REQUIRED"

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled    = true
      start_time = "01:00"
      # Point-in-time recovery stays off to keep the bill down; daily backups only.
      point_in_time_recovery_enabled = false

      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    # Sunday 02:00 UTC. `day` must be set, or create fails on provider 8.6.
    maintenance_window {
      day          = 7
      hour         = 2
      update_track = "stable"
    }

    location_preference {
      zone = var.zone
    }
  }

  lifecycle {
    # Autoresize grows the disk; a smaller value here would try to recreate the instance.
    ignore_changes = [settings[0].disk_size]
  }
}

resource "google_sql_database" "kalkan" {
  name            = local.db_name
  instance        = google_sql_database_instance.main.name
  deletion_policy = "ABANDON"
}

# Passwords live only in Secret Manager: ephemeral values reach Cloud SQL and the secrets through
# write-only arguments, never the plan or the state (K78). kalkan_api and kalkan_worker are
# created by bootstrap_roles.sql (db-bootstrap on the VM), which reads their passwords back.
ephemeral "random_password" "db" {
  for_each = toset(["owner", "api", "worker"])

  length  = 40
  special = false
}

resource "google_sql_user" "kalkan_owner" {
  name                = "kalkan_owner"
  instance            = google_sql_database_instance.main.name
  password_wo         = ephemeral.random_password.db["owner"].result
  password_wo_version = var.db_password_version
  # Cloud SQL cannot drop a role that owns objects; removing it from Terraform leaves it in place.
  deletion_policy = "ABANDON"
}
