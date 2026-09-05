cluster_name     = "homelab"
cluster_endpoint = "192.168.10.110"

cloudflare_account_id       = "db189f6278d9d9fbdfd8dbf99a5e8c95"
cloudflare_zone_id          = "ef642a36cc3c9d8a9e3f757561fa0ce8"
cloudflare_access_policy_id = "cb1bf754-ee1f-44e6-96e5-d51885fe3684"

proxmox_nodes = {
  main     = { ssh_address = "main" }
  data     = { ssh_address = "data" }
  toliunit = { ssh_address = "toliunit" }
}

control_planes = {
  cp-1 = {
    host_node    = "main"
    vm_id        = 1000
    ip           = "192.168.10.110"
    cpu          = 4
    ram          = 24576 # 24GB - schedulable CP, ワークロード実行兼用
    disk_size    = 128 # mainworker-1 退避で空いた toshibassd を充当
    datastore_id = "toshibassd"
  }
}

workers = {
  worker-1 = {
    host_node    = "data"
    vm_id        = 1010
    ip           = "192.168.10.120"
    cpu          = 2
    ram          = 6144 # 6GB - Longhornストレージ + ワークロード兼任
    disk_size    = 450
    datastore_id = "data-pve"
  }
  worker-2 = {
    host_node    = "data"
    vm_id        = 1011
    ip           = "192.168.10.121"
    cpu          = 2
    ram          = 6144 # 6GB - Longhornストレージ + ワークロード兼任
    disk_size    = 350
    datastore_id = "data-pve"
  }
  toliworker-1 = {
    host_node    = "toliunit"
    vm_id        = 1013
    ip           = "192.168.10.123"
    cpu          = 22
    ram          = 20480 # 20GB
    disk_size    = 60
    datastore_id = "ssd0"
    extra_disks = [
      { datastore_id = "ssd", size = 420 },
    ]
  }
  toliworker-2 = {
    host_node    = "toliunit"
    vm_id        = 1014
    ip           = "192.168.10.124"
    cpu          = 22
    ram          = 20480 # 20GB
    disk_size    = 60
    datastore_id = "ssd0"
    extra_disks = [
      { datastore_id = "ssd2", size = 420 },
    ]
  }
  toliworker-3 = {
    host_node    = "toliunit"
    vm_id        = 1015
    ip           = "192.168.10.125"
    cpu          = 22
    ram          = 20480 # 20GB
    disk_size    = 60
    datastore_id = "ssd0"
    extra_disks = [
      { datastore_id = "ssd3", size = 420 },
    ]
  }
  mainworker-1 = {
    host_node    = "main"
    vm_id        = 1016
    ip           = "192.168.10.126"
    cpu          = 8
    ram          = 32768 # 32GB
    disk_size    = 800
    datastore_id = "crucialssd"
  }
}
