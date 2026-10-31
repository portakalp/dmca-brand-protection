tflint {
  required_version = ">= 0.64.0"
}

config {
  call_module_type = "none"
}

plugin "terraform" {
  enabled = true
  preset  = "all"
}

# deep_check stays off: it calls Google Cloud APIs and needs credentials.
plugin "google" {
  enabled = true
  version = "0.40.0"
  source  = "github.com/terraform-linters/tflint-ruleset-google"
}
