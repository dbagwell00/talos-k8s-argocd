# main.tf
resource "proxmox_virtual_environment_vm" "talos_vm" {
  count = length(var.proxmox_nodes)

  name        = "${var.cluster_name}-${count.index + 1}"
  description = "Talos Linux VM ${count.index + 1}"
  node_name   = var.proxmox_nodes[count.index]
  vm_id       = 8000 + count.index + 1

  on_boot = true

  cpu {
    cores = var.vm_cores
    type  = "host"
  }

  memory {
    dedicated = var.vm_memory_mb[count.index]
  }

  agent {
    enabled = true
  }

  # External network (VLAN 3) - 192.168.4.0/24
  network_device {
    bridge      = "vmbr0"
    vlan_id     = var.k8s_external_vlan
    mac_address = "BC:24:11:3E:4${count.index}:00"
  }

  # Internal network (VLAN 2) - 192.168.3.0/24
  network_device {
    bridge      = "vmbr0"
    vlan_id     = var.k8s_internal_vlan
    mac_address = "BC:24:11:2E:4${count.index}:00"
  }

  disk {
    datastore_id = var.vm_storage
    file_id      = proxmox_virtual_environment_download_file.talos_image[var.proxmox_nodes[count.index]].id
    interface    = "virtio0"
    size         = 100
    discard      = "on"
    # No `ssd`: Proxmox ignores the flag on virtio disks, so setting it
    # only produced a permanent diff.
  }

  boot_order = ["virtio0"]

  operating_system {
    type = "l26"
  }

  serial_device {}

  lifecycle {
    ignore_changes = [
      network_device,
      # The image a disk was first created from. Talos upgrades happen in
      # place via `talosctl upgrade`, so a new image (or a VM that moved
      # hosts, like 8002) must never force the VM to be recreated.
      disk[0].file_id,
    ]
  }
}
