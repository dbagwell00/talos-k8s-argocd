# outputs.tf
output "vm_details" {
  description = "Details of Talos VMs"
  value = {
    for vm in proxmox_virtual_environment_vm.talos_vm :
    vm.name => {
      node = vm.node_name
      vm_id = vm.vm_id
      ips = vm.ipv4_addresses
    }
  }
}
