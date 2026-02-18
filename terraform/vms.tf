resource "proxmox_virtual_environment_vm" "talos_node" {
  for_each = local.all_nodes

  name      = each.key
  node_name = each.value.host_node
  vm_id     = each.value.vm_id

  machine       = "q35"
  scsi_hardware = "virtio-scsi-single"
  bios          = "seabios"

  tags = sort([
    "terraform",
    "talos",
    "k8s",
    contains(keys(var.control_planes), each.key) ? "controlplane" : "worker",
  ])

  cpu {
    cores = each.value.cpu
    type  = "host"
  }

  memory {
    dedicated = each.value.ram
  }

  disk {
    datastore_id = each.value.datastore_id
    file_id      = proxmox_virtual_environment_download_file.talos_image[each.value.host_node].id
    interface    = "scsi0"
    iothread     = true
    discard      = "on"
    size         = each.value.disk_size
    file_format  = "raw"
  }

  network_device {
    bridge = "vmbr0"
  }

  initialization {
    datastore_id = each.value.datastore_id

    ip_config {
      ipv4 {
        address = "${each.value.ip}/24"
        gateway = var.gateway
      }
    }
  }

  operating_system {
    type = "l26"
  }

  agent {
    enabled = true
  }

  lifecycle {
    ignore_changes = [initialization]
  }
}
