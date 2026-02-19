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
            # Secret management
            sops
            age
          ];
          env = {
            SOPS_AGE_KEY_CMD = "command -v rbw >/dev/null 2>&1 && rbw get infra-age-key";
          };
          shellHook = ''
            export TF_VAR_state_encryption_passphrase="$(command -v rbw >/dev/null 2>&1 && rbw get infra-tohu-state-passphrase)"
            export PROXMOX_VE_API_TOKEN="$(command -v rbw >/dev/null 2>&1 && rbw get proxmox-tohu-token)"
          '';
        };
      }
    );
}
