# Reference deployment on Google Cloud (architecture.md §22). One root, no modules; you run every
# plan and apply yourself (README.md). Resources are split by service into the other files.

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # Bill API quota to this project when Terraform runs with gcloud user credentials (ADC).
  user_project_override = true
  billing_project       = var.project_id
}

locals {
  vm_name  = "kalkan-vm"
  vm_tag   = "kalkan-vm"
  db_name  = "kalkan"
  api_host = trimprefix(google_cloud_run_v2_service.api.uri, "https://")
}
