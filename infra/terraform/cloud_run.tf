# Cloud Run api (§3, §22; K79). Terraform creates the service with a placeholder image; the
# deploy owns the image, port, memory, cpu, env vars, secrets and the Cloud SQL attachment, which
# Terraform then ignores so a plan never reverts a deploy. Scaling stays in Terraform. The service
# is public (no invoker IAM check): the app rejects requests without the edge Worker's X-Edge-Auth
# header, except the health paths. The web service (the customer panel, §18) follows the same
# pattern below.

resource "google_cloud_run_v2_service" "api" {
  name                 = "api"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true
  deletion_protection  = true

  template {
    service_account = google_service_account.kalkan["api"].email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }

    # Create-only: ignore_changes covers the containers, so the image, port, resources and mounts
    # below apply only when the service is created and editing them here changes nothing. The
    # deploy sets the port, memory and cpu (README.md, Deploying).
    containers {
      # Placeholder until the first deploy; it listens on $PORT.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      ports {
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers,
      template[0].volumes,
      template[0].annotations,
      template[0].labels,
      template[0].revision,
    ]
  }

  depends_on = [google_project_iam_member.kalkan]
}

# Cloud Run web: the Next.js panel (§18; K157). Same split as api: Terraform creates it with
# the placeholder image and keeps the scaling; the deploy owns the image, port and the
# EDGE_SHARED_SECRET binding. No database, no buckets: the browser talks to the API through the
# edge Worker, and the panel only refuses requests without X-Edge-Auth.
resource "google_cloud_run_v2_service" "web" {
  name                 = "web"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true
  deletion_protection  = true

  template {
    service_account = google_service_account.kalkan["web"].email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    # Create-only, as for api (ignore_changes below).
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      ports {
        container_port = 3000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers,
      template[0].annotations,
      template[0].labels,
      template[0].revision,
    ]
  }

  depends_on = [google_project_iam_member.kalkan]
}

# Cloud Run's own request log records the full URL, and /e/<token> is a 90-day bearer link to
# evidence (K35, K84). The Worker's invocation logs are off and the app masks the token; this
# exclusion keeps Cloud Run's request entries for those URLs out of the _Default sink.
resource "google_logging_project_exclusion" "evidence_links" {
  name        = "kalkan-api-evidence-links"
  description = "Cloud Run api request logs of /e/<token> evidence links"
  filter      = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${google_cloud_run_v2_service.api.name}\" AND log_id(\"run.googleapis.com/requests\") AND httpRequest.requestUrl=~\"/e/\""
}
