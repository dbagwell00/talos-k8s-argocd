# mesh.tf
# The talos-mesh cluster's three control-plane VMs. They were built by hand
# and imported here on 2026-09-24 (see the import block at the bottom).
#
# Unlike talos-cilium, mesh nodes get static addresses through a cloud-init
# drive (ide2) and sit on VLANs 5 (192.168.6.0/24) and 4 (192.168.5.0/24).
# Their etcd lives on local-lvm (NVMe); it was moved off Ceph RBD because
# fsync latency there (~100 ms) cost the cluster its leader leases.

variable "mesh_cluster_name" {
  description = "talos-mesh cluster name"
  type        = string
  default     = "talos-mesh"
}

variable "mesh_vms" {
  description = "talos-mesh VMs keyed by VM ID"
  type = map(object({
    index     = number # 1-based; used in the name and description
    node      = string
    external  = string # VLAN 5, 192.168.6.0/24 (default route)
    internal  = string # VLAN 4, 192.168.5.0/24
    mac_index = number # BC:24:11:4E:5<n>:00 / BC:24:11:5E:5<n>:00
  }))
  default = {
    "9001" = { index = 1, node = "prox01", external = "192.168.6.20", internal = "192.168.5.20", mac_index = 0 }
    "9002" = { index = 2, node = "prox02", external = "192.168.6.22", internal = "192.168.5.22", mac_index = 1 }
    "9003" = { index = 3, node = "prox04", external = "192.168.6.24", internal = "192.168.5.24", mac_index = 2 }
  }
}

variable "mesh_vm_cores" {
  description = "vCPUs per talos-mesh VM"
  type        = number
  default     = 6
}

variable "mesh_vm_memory_mb" {
  description = "Memory (MB) per talos-mesh VM"
  type        = number
  default     = 16384
}

resource "proxmox_virtual_environment_vm" "talos_mesh_vm" {
  for_each = var.mesh_vms

  name        = "${var.mesh_cluster_name}-${each.value.index}"
  description = "Talos Linux VM ${each.value.index} for ${var.mesh_cluster_name}"
  node_name   = each.value.node
  vm_id       = tonumber(each.key)

  on_boot = true

  cpu {
    cores = var.mesh_vm_cores
    type  = "host"
  }

  memory {
    dedicated = var.mesh_vm_memory_mb
  }

  agent {
    enabled = true
  }

  # External network (VLAN 5) - 192.168.6.0/24
  network_device {
    bridge      = "vmbr0"
    vlan_id     = 5
    mac_address = "BC:24:11:4E:5${each.value.mac_index}:00"
  }

  # Internal network (VLAN 4) - 192.168.5.0/24
  network_device {
    bridge      = "vmbr0"
    vlan_id     = 4
    mac_address = "BC:24:11:5E:5${each.value.mac_index}:00"
  }

  disk {
    datastore_id = var.vm_storage
    file_id      = proxmox_virtual_environment_download_file.talos_image[each.value.node].id
    interface    = "virtio0"
    size         = 100
    discard      = "on"
  }

  initialization {
    datastore_id = var.vm_storage
    interface    = "ide2"

    ip_config {
      ipv4 {
        address = "${each.value.external}/24"
        gateway = "192.168.6.1"
      }
    }

    ip_config {
      ipv4 {
        address = "${each.value.internal}/24"
      }
    }

    dns {
      servers = ["192.168.1.1", "1.1.1.1"]
    }
  }

  boot_order = ["virtio0"]

  operating_system {
    type = "l26"
  }

  serial_device {}

  lifecycle {
    ignore_changes = [
      network_device,
      # Imported VMs have no record of their source image, and Talos upgrades
      # happen in place, so the image must never force a rebuild.
      disk[0].file_id,
    ]
  }
}

import {
  for_each = var.mesh_vms
  to       = proxmox_virtual_environment_vm.talos_mesh_vm[each.key]
  id       = "${each.value.node}/${each.key}"
}
