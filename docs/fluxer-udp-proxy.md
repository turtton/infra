# Fluxer voice UDP path

Fluxer voice is paused while the old LiveKit credentials and Funnel are retired.
This runbook prepares one media path without enabling voice:

```text
Browser -- UDP/7882 --> Sakura VPS (NGINX stream)
                         -- Tailscale UDP/7882 --> toliworker-1 --> LiveKit
```

The VPS proxy listens on its public interface and forwards to the current
Tailscale address of `toliworker-1`. The LiveKit pod must later run on that
node with host networking and `rtc.udp_port: 7882`; the existing Caddy route
can then carry only WebSocket signaling. The new Tailscale grant adds
`tag:mcproxy` to `tag:infra-talos-cluster` on UDP/7882. The Attic Funnel is
unrelated and stays enabled.

## Apply in order

1. Confirm `toliworker-1` still owns the upstream Tailscale address in the
   encrypted VPS host inventory. On Talos, run `talosctl get addresses` for
   that node and inspect `tailscale0`. Update the SOPS inventory if the node
   has been re-enrolled.
2. Review the Terraform ACL change locally. After merge, run **OpenTofu Apply**
   on `main` with `scope=tailscale-acl` in `plan` mode, then `apply` mode. This
   exceptional target is intended to avoid applying unrelated drift. Review
   the remaining changes separately with a full plan afterward.
3. Check the Sakura VPS packet filter. If enabled, allow public UDP/7882.
   Ansible only manages the VPS nftables rules, not the Sakura control panel.
4. Run **Ansible Apply** on `main` with `vps.yml` in `check` mode, then `apply`
   mode. The role installs NGINX with its stream module, listens on UDP/7882,
   and forwards to the selected Talos node. TCP/80 is not opened in nftables.
5. On the VPS, verify `nginx -t`, `ss -ulnp | grep 7882`, and the nftables
   input rule. From the VPS, verify the Tailscale peer is online. Keep Fluxer
   voice disabled until LiveKit has fresh credentials and the end-to-end test
   below passes.

## LiveKit cutover prerequisites

- Generate a new LiveKit API key and secret. Store them only in a SOPS Secret;
  both Fluxer API and the LiveKit HelmRelease must use the same new values.
- Configure LiveKit with a single UDP mux port (`rtc.udp_port: 7882`), disable
  the old TCP and TURN Funnel paths, advertise the VPS public IPv4 as
  `rtc.node_ip`, and set `rtc.use_external_ip: false`.
- Pin the LiveKit pod to `toliworker-1` for this initial host-network path.
  The node is a single point of failure. A later NodePort or multi-node design
  can remove this limitation after media traffic is proven.
- Restore Caddy `/livekit` signaling only after the server and API are ready.

Test with two non-tailnet devices on separate networks. Confirm connection,
two-way audio, selected WebRTC ICE candidate, VPS receive/transmit packets,
and recovery after LiveKit restart. If no packets reach the VPS, inspect the
Sakura packet filter. If packets reach the VPS but not LiveKit, inspect the
NGINX upstream, Tailscale grant, and node port. If the Pod moves away from
`toliworker-1`, voice stops until it is rescheduled there.

Rollback is to disable Fluxer voice and the Caddy `/livekit` route, then remove
the NGINX role and UDP/7882 nftables allowance from the VPS playbook/inventory.
