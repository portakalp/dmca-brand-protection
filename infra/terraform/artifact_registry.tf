# Docker images backend, browser and web, tagged with the full git SHA.
# Tags stay mutable so re-running a deploy for the same commit can push again.
resource "google_artifact_registry_repository" "kalkan" {
  location      = var.region
  repository_id = "kalkan"
  format        = "DOCKER"
  description   = "Kalkan images, tagged with the git SHA"
}
