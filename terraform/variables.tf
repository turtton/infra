variable "state_encryption_passphrase" {
  description = "Passphrase for OpenTofu state encryption (PBKDF2+AES-GCM)"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.state_encryption_passphrase) >= 16
    error_message = "State encryption passphrase must be at least 16 characters."
  }
}

variable "tailscale_authkey" {
  description = "Tailscale auth key for Talos nodes (reusable, tagged)"
  type        = string
  sensitive   = true
}

variable "tailscale_oauth_client_id" {
  description = "Tailscale OAuth client ID for ACL management"
  type        = string
  sensitive   = true
}

variable "tailscale_oauth_client_secret" {
  description = "Tailscale OAuth client secret for ACL management"
  type        = string
  sensitive   = true
}

variable "tailscale_tailnet" {
  description = "Tailscale tailnet name (e.g. example.com or user@gmail.com)"
  type        = string
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token for Tunnel/Access/DNS management"
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID"
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID for turtton.net"
  type        = string
}

variable "cloudflare_access_policy_id" {
  description = "Cloudflare Zero Trust reusable access policy ID"
  type        = string
}

variable "cluster_name" {
  description = "Kubernetes cluster name"
  type        = string
  default     = "homelab"
}

variable "cluster_endpoint" {
  description = "Kubernetes API endpoint IP address"
  type        = string
}

variable "talos_version" {
  description = "Talos Linux version"
  type        = string
  default     = "v1.12.1"
}

variable "kubernetes_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.35.0"
}

variable "proxmox_nodes" {
  description = "Proxmox node SSH addresses (Tailscale FQDN)"
  type = map(object({
    ssh_address = string
  }))
}

variable "gateway" {
  description = "Default gateway IP"
  type        = string
  default     = "192.168.10.1"
}

variable "control_planes" {
  description = "Control plane node definitions"
  type = map(object({
    host_node    = string
    vm_id        = number
    ip           = string
    cpu          = number
    ram          = number
    disk_size    = number
    datastore_id = string
    extra_disks = optional(list(object({
      datastore_id = string
      size         = number
    })), [])
  }))
}

variable "workers" {
  description = "Worker node definitions"
  type = map(object({
    host_node    = string
    vm_id        = number
    ip           = string
    cpu          = number
    ram          = number
    disk_size    = number
    datastore_id = string
    extra_disks = optional(list(object({
      datastore_id = string
      size         = number
    })), [])
  }))
}
