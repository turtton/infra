{
  description = "A basic flake with a shell";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  inputs.systems.url = "github:nix-systems/default";
  inputs.flake-utils = {
    url = "github:numtide/flake-utils";
    inputs.systems.follows = "systems";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        formatter = pkgs.nixfmt-tree;
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            bashInteractive
            # Ansible
            ansible
            ansible-lint
            # OpenTofu
            opentofu
            # Kubernetes / Talos
            talosctl
            kubectl
            # Flux CD
            fluxcd
            # Helm
            kubernetes-helm
            # Secret management
            sops
            age
          ];
          env = {
            SOPS_AGE_KEY_CMD = "rbw get infra-age-key";
          };
          shellHook = ''
            rbw_get() { command -v rbw >/dev/null 2>&1 && rbw get "$1" 2>/dev/null; }
            rbw_get_username() { command -v rbw >/dev/null 2>&1 && rbw get -f username "$1" 2>/dev/null; }
            val="$(rbw_get infra-tohu-state-passphrase)"             && export TF_VAR_state_encryption_passphrase="$val"
            val="$(rbw_get terraform-tailscale-auth-key)"            && export TF_VAR_tailscale_authkey="$val"
            val="$(rbw_get terraform-cloudflare-api-token)"          && export TF_VAR_cloudflare_api_token="$val"
            val="$(rbw_get_username terraform-acl-tailscale-oauth)"  && export TF_VAR_tailscale_oauth_client_id="$val"
            val="$(rbw_get terraform-acl-tailscale-oauth)"           && export TF_VAR_tailscale_oauth_client_secret="$val"
            val="$(rbw_get tailscale-tailnet-id)"                    && export TF_VAR_tailscale_tailnet="$val"
            val="$(rbw_get proxmox-tohu-token)"                      && export PROXMOX_VE_API_TOKEN="$val"
            unset -f rbw_get; unset val
          '';
        };
      }
    );
}
