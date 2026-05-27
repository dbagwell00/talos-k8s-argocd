# variables.tf
variable "proxmox_api_url" {
  description = "Proxmox API URL"
  type        = string
}

variable "proxmox_api_token_id" {
  description = "Proxmox API Token ID"
  type        = string
}

variable "proxmox_api_token_secret" {
  description = "Proxmox API Token Secret"
  type        = string
  sensitive   = true
}

variable "proxmox_nodes" {
  description = "List of Proxmox nodes"
  type        = list(string)
  default     = ["prox01", "prox04", "prox03", "prox04"]
}

variable "talos_version" {
  description = "Talos version"
  type        = string
  default     = "v1.8.3"
}

variable "cluster_name" {
  description = "Talos cluster name"
  type        = string
  default     = "talos-cilium"
}

variable "vm_storage" {
  description = "Storage for VM disks"
  type        = string
  default     = "local-lvm"
}

variable "k8s_external_vlan" {
  description = "VLAN ID for k8s-external network"
  type        = number
  default     = 3
}

variable "k8s_internal_vlan" {
  description = "VLAN ID for k8s-internal network"
  type        = number
  default     = 2
}
