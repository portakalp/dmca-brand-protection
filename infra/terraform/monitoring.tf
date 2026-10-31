# Cloud Monitoring uptime check on the api's run.app address (§22, §23; K80, K83).
# /api/health needs no edge header; Cloud Run reserves some paths ending in "z", such as /healthz.
# Below are also the edge checks (app and evidence through Cloudflare) and the backup alarm; the
# alert rules on the VM live in infra/compose/vm/prometheus/rules (K173, K174).

resource "google_monitoring_notification_channel" "ops_email" {
  display_name = "Kalkan ops e-mail"
  type         = "email"

  labels = {
    email_address = var.ops_alert_email
  }
}

resource "google_monitoring_uptime_check_config" "api" {
  display_name = "kalkan api /api/health"
  period       = "300s"
  timeout      = "10s"

  http_check {
    path           = "/api/health"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.api_host
    }
  }
}

# Fires when the check fails from more than one checker region for five minutes.
resource "google_monitoring_alert_policy" "api_uptime" {
  display_name          = "kalkan api is down"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.ops_email.name]

  conditions {
    display_name = "api /api/health uptime check failing"

    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.api.uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"

      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "The Cloud Run api does not answer GET /api/health with 2xx. See \"Logs\" and \"Rollback\" in docs/operations.md."
  }
}

# The two public hosts through the edge Worker (K80, K84): /healthz goes to the api's /api/health.
# A failure here and not in the api check points at Cloudflare, DNS or the Worker.
locals {
  edge_hosts = {
    app      = "app.${var.domain}"
    evidence = "evidence.${var.domain}"
  }
}

resource "google_monitoring_uptime_check_config" "edge" {
  for_each = local.edge_hosts

  display_name = "kalkan ${each.key} /healthz"
  period       = "300s"
  timeout      = "10s"

  http_check {
    path           = "/healthz"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = each.value
    }
  }
}

resource "google_monitoring_alert_policy" "edge_uptime" {
  for_each = local.edge_hosts

  display_name          = "kalkan ${each.key} is down"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.ops_email.name]

  conditions {
    display_name = "${each.value}/healthz uptime check failing"

    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.edge[each.key].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"

      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "https://${each.value}/healthz does not answer 2xx through Cloudflare. If the api check is green, look at the Worker, DNS and the Custom Domain. See \"Incidents\" in docs/operations.md."
  }
}

# Cloud SQL writes one System Event audit log entry per automated backup window
# (cloudsql.instances.automatedBackup, protoPayload.metadata.windowStatus); System Event logs are
# always on. Prometheus cannot see the backups, so this alarm lives here (K174).
resource "google_monitoring_alert_policy" "backup_failed" {
  display_name          = "kalkan database backup failed"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.ops_email.name]

  conditions {
    display_name = "Cloud SQL automated backup not succeeded"

    condition_matched_log {
      filter = join(" AND ", [
        "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Fsystem_event\"",
        "protoPayload.methodName=\"cloudsql.instances.automatedBackup\"",
        "resource.type=\"cloudsql_database\"",
        "protoPayload.metadata.windowStatus!=\"STATUS_SUCCEEDED\"",
      ])
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = "A Cloud SQL automated backup of kalkan-db did not succeed (STATUS_ATTEMPT_FAILED, STATUS_FAILED or STATUS_SKIPPED; the log entry's protoPayload.metadata.message says why). Take an on-demand backup and see \"Incidents\" in docs/operations.md."
  }
}
