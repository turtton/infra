# Fluxer voice UDP path

Fluxer voice uses a VPS UDP media path. The retired LiveKit Funnel stays disabled:

```text
Browser -- UDP/7882 --> Sakura VPS (NGINX stream)
                         -- Tailscale UDP/30082 --> toliworker-1 --> LiveKit UDP/7882
```

The VPS proxy listens on its public interface and forwards to the current
Tailscale address of `toliworker-1`. The LiveKit pod runs on that
node behind a UDP NodePort and uses `rtc.udp_port: 7882`; the existing Caddy route
can then carry only WebSocket signaling. The new Tailscale grant adds
`tag:mcproxy` to `tag:infra-talos-cluster` on UDP/30082. The Attic Funnel is
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
4. Run **Ansible Apply** on `main` with `vps-livekit.yml` in `check` mode, then
   `apply` mode. The role installs NGINX with its stream module, listens on UDP/7882,
   and forwards to the selected Talos node. TCP/80 is not opened in nftables.
5. On the VPS, verify `nginx -t`, `ss -ulnp | grep 7882`, and the nftables
   input rule. From the VPS, verify the Tailscale peer is online. Verify fresh
   LiveKit credentials and the end-to-end media path before enabling voice.

The **Ansible Apply** workflow can run `vps-udp-probe.yml` with `mode=probe`.
It sends a marker packet from a GitHub runner outside the home network and
captures it on the VPS public interface and the Tailscale interface. A failure
at "Verify public UDP reached the VPS" points to the Sakura packet filter or
the runner's outbound path. A failure at "Verify UDP was forwarded into
Tailscale" points to the VPS proxy or local firewall. If both pass, continue
with the NodePort and LiveKit checks. The test does not verify return traffic
or two-way audio.

## LiveKit cutover prerequisites

- Generate a new LiveKit API key and secret. Store them only in a SOPS Secret;
  both Fluxer API and the LiveKit HelmRelease must use the same new values.
- Configure LiveKit with a single UDP mux port (`rtc.udp_port: 7882`), disable
  the old TCP and TURN Funnel paths, advertise the VPS public IPv4 as
  `rtc.node_ip`, and set `rtc.use_external_ip: false`.
- Pin the LiveKit pod to `toliworker-1` for the initial NodePort path.
  The node is a single point of failure. A multi-node design can remove this
  limitation after media traffic is proven.
- Restore Caddy `/livekit` signaling only after the server and API are ready.

The server manifest and rotated credentials are prepared in
`clusters/main/apps/fluxer/livekit-helmrelease.yaml` and
`livekit-keys.sops.yaml`. Verify the HelmRelease and pod are healthy before
enabling the Fluxer API or Caddy route. The API secret references must point
to `livekit-keys` when voice is enabled.

The public UDP probe, direct LiveKit ICE connection, and synthetic audio between
two clients passed after the Sakura packet filter was updated. Test the Fluxer
browser UI with two non-tailnet devices on separate networks. Confirm connection,
two-way audio, selected WebRTC ICE candidate, VPS receive/transmit packets,
and recovery after LiveKit restart. If no packets reach the VPS, inspect the
Sakura packet filter. If packets reach the VPS but not LiveKit, inspect the
NGINX upstream, Tailscale grant, and node port. If the Pod moves away from
`toliworker-1`, voice stops until it is rescheduled there.

Rollback is to disable Fluxer voice and the Caddy `/livekit` route, then remove
the NGINX role and UDP/7882 nftables allowance from the VPS playbook/inventory.
