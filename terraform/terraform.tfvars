cluster_name     = "homelab"
cluster_endpoint = "192.168.11.110"

proxmox_nodes = {
  main = { ssh_address = "main" }
  data = { ssh_address = "data" }
}

control_planes = {
  cp-1 = {
    host_node    = "main"
    vm_id        = 1000
    ip           = "192.168.11.110"
    cpu          = 4
    ram          = 24576 # 24GB - schedulable CP, ワークロード実行兼用
    disk_size    = 32
    datastore_id = "toshibassd"
  }
}

workers = {
  worker-1 = {
    host_node    = "data"
    vm_id        = 1010
    ip           = "192.168.11.120"
    cpu          = 1
    ram          = 4096 # 4GB - Longhornストレージ専用
    disk_size    = 100
    datastore_id = "data-pve"
  }
  worker-2 = {
    host_node    = "data"
    vm_id        = 1011
    ip           = "192.168.11.121"
    cpu          = 1
    ram          = 4096 # 4GB - Longhornストレージ専用
    disk_size    = 350
    datastore_id = "data-pve"
  }
  worker-3 = {
    host_node    = "data"
    vm_id        = 1012
    ip           = "192.168.11.122"
    cpu          = 1
    ram          = 4096 # 4GB - Longhornストレージ専用
    disk_size    = 350
    datastore_id = "data-pve"
  }
}
