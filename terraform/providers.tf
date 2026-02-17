provider "proxmox" {
  # PROXMOX_VE_ENDPOINT, PROXMOX_VE_API_TOKEN 環境変数で設定
  insecure = true # 自己署名証明書

  ssh {
    agent       = false
    username    = "root"
    private_key = file(pathexpand("~/.ssh/id_ed25519"))
  }
}

provider "talos" {}
