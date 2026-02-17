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
  default     = "v1.9.5"
}

variable "kubernetes_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.32.3"
}

variable "gateway" {
  description = "Default gateway IP"
  type        = string
  default     = "192.168.11.1"
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
  }))
}
