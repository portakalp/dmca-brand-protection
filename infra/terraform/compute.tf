# Worker VM (§3, §22; K81). The startup script installs Docker, the gcloud CLI and the systemd
# units at every boot; deploys ship the compose bundle over IAP SSH. Inbound traffic is SSH from
# IAP only (network.tf).

resource "google_compute_instance" "vm" {
  name                      = local.vm_name
  machine_type              = "e2-standard-2"
  zone                      = var.zone
  tags                      = [local.vm_tag]
  deletion_protection       = true
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 30
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.kalkan.id

    # Ephemeral external IPv4 for egress (no Cloud NAT); the firewall admits nothing on it.
    access_config {}
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    # The startup-script key updates in place (metadata_startup_script would recreate the VM)
    # and runs at every boot, so the script is idempotent.
    startup-script = file("${path.module}/../vm/startup.sh")
  }

  service_account {
    email  = google_service_account.kalkan["vm"].email
    scopes = ["cloud-platform"]
  }

  lifecycle {
    # The image family moves on; a new image must not replace the boot disk.
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }

  # The first boot pulls from Artifact Registry and logs through gcplogs.
  depends_on = [
    google_project_iam_member.kalkan,
    google_artifact_registry_repository_iam_member.vm_reader,
  ]
}
