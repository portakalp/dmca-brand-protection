# Dedicated VPC (K77). The project's default network, whose rules allow SSH, RDP and ICMP
# from anywhere, is not used and not touched.

resource "google_compute_network" "kalkan" {
  name                    = "kalkan"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "kalkan" {
  name                     = "kalkan-${var.region}"
  region                   = var.region
  network                  = google_compute_network.kalkan.id
  ip_cidr_range            = "10.10.0.0/24"
  private_ip_google_access = true
}

# The only inbound rule: SSH from IAP's TCP forwarding range (§22). Everything else is denied by
# the VPC's implied ingress rule; the VM's external IP is for egress only.
resource "google_compute_firewall" "iap_ssh" {
  name          = "kalkan-allow-iap-ssh"
  network       = google_compute_network.kalkan.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.vm_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}
