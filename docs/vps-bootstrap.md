# Sakura VPS Bootstrap and Recovery Runbook

This document describes how to bootstrap the Sakura VPS (`os3-387-26840`) for Ansible management, and how to recover from lockouts.

## Prerequisites

- Server identity: `os3-387-26840`
- Bootstrap public IP: `133.167.115.94`
- OS: Debian 12 bookworm (amd64)
- Initial user: `debian` (Sakura VPS default, passwordless sudo not enabled by default)
- Management user: `ansible`
- Management network: Tailscale (`sakura-mcproxy-1`, `tag:mcproxy`)
- Control machine: a host that can reach the bootstrap IP and has the matching SSH private key

## Bootstrap Procedure

1. Open the Sakura control panel and confirm console or VNC access.
2. Identify the initial root authentication method (root password or pre-installed SSH key).
3. If needed, use the console to reset the root password.
4. Optionally enable Sakura Packet Filter for TCP/22 during bootstrap.
5. Record the SSH host key fingerprint from the Sakura console or by connecting once and verifying it out-of-band.
6. Add the public IP host key to your `known_hosts`.
7. Ensure the public key matching the GitHub Actions secret `SSH_PRIVATE_KEY` is set in `ansible/inventory/group_vars/vps/main.yml` under `vps_admin_authorized_keys`.
8. Run the bootstrap playbook. The default Sakura VPS user is `debian`; provide the sudo password when prompted:

```bash
ANSIBLE_HOST_KEY_CHECKING=True \
ansible-playbook playbooks/bootstrap-vps.yml \
  -i inventory/hosts.yml \
  --limit os3-387-26840 \
  --ask-become-pass \
  --extra-vars "ansible_host=133.167.115.94"
```

If the `debian` user has passwordless sudo or you prefer to use an SSH key for `debian`:

```bash
ANSIBLE_HOST_KEY_CHECKING=True \
ansible-playbook playbooks/bootstrap-vps.yml \
  -i inventory/hosts.yml \
  --limit os3-387-26840 \
  --private-key /path/to/debian-key \
  --extra-vars "ansible_host=133.167.115.94"
```

9. Verify SSH as `ansible`:

```bash
ANSIBLE_HOST_KEY_CHECKING=True \
ssh -i /path/to/private-key ansible@133.167.115.94
```

10. Verify passwordless sudo:

```bash
ssh -i /path/to/private-key ansible@133.167.115.94 sudo -n true
```

11. Add the Tailscale MagicDNS host key to `known_hosts`.
12. Continue with the normal `vps.yml` playbook over Tailscale.

## Recovery Procedures

### If SSH as ansible fails

1. Open the Sakura console or VNC.
2. Log in as root (reset the password via console if necessary).
3. Check `/home/ansible/.ssh/authorized_keys` and permissions.
4. Check `/etc/sudoers.d/ansible` and permissions.
5. Re-run the bootstrap playbook.

### If Tailscale fails

1. Use the Sakura console to access the host.
2. Check `tailscale status` and `tailscale up` output.
3. If needed, request a new one-off auth key and re-run the Tailscale role.

### If SSH hardening locks you out

1. Use the Sakura console or VNC.
2. Edit `/etc/ssh/sshd_config.d/hardening.conf` or remove it.
3. Restart sshd.
4. Re-run the appropriate playbook after verifying access.

### If firewall rules block Tailscale SSH

1. Use the Sakura console or VNC.
2. Inspect nftables rules.
3. Stop or correct the Ansible-managed rules.
4. Verify Tailscale SSH works before re-applying hardening.

### If Sakura Packet Filter is disabled too early

1. Re-enable the packet filter from the Sakura control panel.
2. Verify console access and recovery path.
3. Re-apply the full verification sequence before disabling it again.

## Credentials

No credentials are stored in this repository. Secrets are managed via SOPS and age in `ansible/inventory/group_vars/vps/vault.sops.yml`.

## Notes

- The bootstrap playbook refuses to run without `--limit os3-387-26840`.
- Public keys are stored in `ansible/inventory/group_vars/vps/main.yml`.
- Private keys, root passwords, and age keys must never be committed.
