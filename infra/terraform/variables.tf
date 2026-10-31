variable "project_id" {
  description = "Google Cloud project ID, e.g. my-gcp-project."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a Google Cloud project ID (6-30 characters, lowercase letters, digits, hyphens)."
  }
}

variable "region" {
  description = "Region for every regional resource."
  type        = string
  default     = "europe-central2"
}

variable "zone" {
  description = "Zone for the VM and the Cloud SQL instance; must be in var.region."
  type        = string
  default     = "europe-central2-a"

  validation {
    condition     = startswith(var.zone, "${var.region}-")
    error_message = "zone must be a zone of var.region."
  }
}

variable "github_repo" {
  description = "GitHub repository (owner/name) that deploys, e.g. my-org/kalkan; used in the WIF condition and the gh commands."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo must look like owner/name."
  }
}

variable "github_repository_id" {
  description = "Numeric id of var.github_repo (gh api repos/portakalp/dmca-brand-protection --jq .id); WIF trusts only this repository (K76)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be numeric."
  }
}

variable "github_deployer_id" {
  description = "Numeric GitHub user id allowed to start deploys (the OIDC actor_id; gh api users/LOGIN --jq .id)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_deployer_id))
    error_message = "github_deployer_id must be numeric."
  }
}

variable "github_deploy_workflow" {
  description = "Path of the deploy workflow in var.github_repo that WIF trusts; write your own (see README.md, Deploying)."
  type        = string
  default     = ".github/workflows/deploy.yml"

  validation {
    condition     = can(regex("^\\.github/workflows/[A-Za-z0-9._-]+\\.ya?ml$", var.github_deploy_workflow))
    error_message = "github_deploy_workflow must be a file under .github/workflows/."
  }
}

variable "domain" {
  description = "Public domain behind Cloudflare; the panel is app.<domain>, evidence pages evidence.<domain>."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain))
    error_message = "domain must be a lowercase host name such as example.com."
  }
}

variable "ops_alert_email" {
  description = "Recipient of the Cloud Monitoring uptime alert (and the OPS_ALERT_EMAIL GitHub variable)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s']+@[^@\\s']+\\.[^@\\s']+$", var.ops_alert_email))
    error_message = "ops_alert_email must be an e-mail address."
  }
}

variable "db_password_version" {
  description = "Bump to rotate the database passwords Terraform writes to Secret Manager (K78); the next deploy resets the role passwords."
  type        = number
  default     = 1

  validation {
    condition     = var.db_password_version >= 1 && floor(var.db_password_version) == var.db_password_version
    error_message = "db_password_version must be a positive integer."
  }
}

variable "redis_password_version" {
  description = "Bump to rotate the VM Redis ACL passwords and REDIS_URLs Terraform writes to Secret Manager (K78, K82); the next deploy applies them on the VM."
  type        = number
  default     = 1

  validation {
    condition     = var.redis_password_version >= 1 && floor(var.redis_password_version) == var.redis_password_version
    error_message = "redis_password_version must be a positive integer."
  }
}
