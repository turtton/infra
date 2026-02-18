cluster_name     = "homelab"
cluster_endpoint = "192.168.11.110"

proxmox_nodes = {
  main = { ssh_address = "main.CHANGEME.ts.net" }
  data = { ssh_address = "data.CHANGEME.ts.net" }
}

control_planes = {
  cp-1 = {
    host_node    = "main"
    vm_id        = 1000
    ip           = "192.168.11.110"
    cpu          = 4
    ram          = 24576 # 24GB - schedulable CP, ワークロード実行兼用
    disk_size    = 6
    datastore_id = "toshibassd"
  }
}

workers = {
  worker-1 = {
    host_node    = "data"
    vm_id        = 1010
    ip           = "192.168.11.120"
    cpu          = 1
    ram          = 2048 # 2GB - Longhornストレージ専用
    disk_size    = 100
    datastore_id = "data-pve"
  }
}
