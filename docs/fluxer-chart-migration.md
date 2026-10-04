# Fluxer chart migration

## Scope

Keep the nine existing Helm releases and move their chart definitions from
`charts/fluxer/legacy` (upstream `0b6edf569086098ba5be58fc4990e5065a56d9ce`)
to `charts/fluxer/current` (upstream `5799ef705dccb993560be1a31e9b129cf5772968`).
Both chart sets use the existing `flux-system` GitRepository. The separate
`fluxer-upstream` source stays suspended to prevent expensive upstream full
Git-history cloning.

This is a chart migration. The application binaries are pinned to the existing
Ready Pod digests. PostgreSQL, SeaweedFS, NATS, Meilisearch, Valkey, Caddy, and
LiveKit definitions and data are not migrated.

| Existing releases | New chart |
| --- | --- |
| fluxer-api | fluxer-api (additional worker disabled) |
| fluxer-admin, fluxer-app-proxy | fluxer-web (one workload per release) |
| fluxer-media-proxy | fluxer-media-proxy (media-proxy and static-proxy only) |
| fluxer-gateway | fluxer-gateway (existing role separation) |
| fluxer-messages, fluxer-users, fluxer-snowflakes, fluxer-unfurl | fluxer-svc (one service per release) |

## Migration safeguards

Every release sets `upgrade.chartNameChangeStrategy: InPlaceUpdate`. The live
helm-controller `v1.6.5` and HelmRelease CRD support this field. The default
`Reinstall` would uninstall and reinstall the release when the chart name changes.

Existing resource names, release ownership, selectors, Service endpoints,
headless Service names, PDB names, and StatefulSet immutable fields are preserved.
The API wrapper is applied directly to its Deployment because the new API chart
ignores the workload command value. Existing environment variables, Secret
references, resources, probes, scheduling, and shutdown settings are retained.
The nonexistent legacy `ghcr-pull-secret` pull reference is removed from svc Pods.

`reconcileStrategy: Revision` allows chart-content updates at the same upstream
chart version. Pod template chart labels are stabilized by postRenderers so an
unrelated infra commit changes the packaged chart revision without restarting
Fluxer Pods.

## Checks before applying

Run the compatibility test and require both PR checks to pass:

```sh
uv run --with pyyaml==6.0.3 python tests/scripts/fluxer-chart-test.py --output-dir /tmp/fluxer-migration-render
kubectl apply --dry-run=server -f /tmp/fluxer-migration-render/all.yaml
```

Record the existing workload UIDs, PVC bindings, release names, and Ready Pod
image digests. Confirm the live CRD still supports `InPlaceUpdate`. Render and
server dry-run are preflight checks; they do not prove an actual Flux upgrade or
browser login, chat, upload, or voice-call behavior.

## Applying and checking

After merging the migration PR, reconcile the apps kustomization with its source.
Wait for all nine Fluxer HelmReleases to be Ready on the new chart revisions and
for every affected Deployment/StatefulSet rollout to complete. Verify the recorded
workload UIDs, release ownership, and PVC bindings remain unchanged.

Check the public UI and `https://chat.turtton.net/.well-known/fluxer`, the Gateway
WebSocket route, media/static routes, and representative login/chat/upload/call
flows. The image references must still resolve to the recorded running digests.

## Rollback

Keep `charts/fluxer/legacy` available. Prepare a rollback PR restoring the previous
HelmRelease values, chart paths, and postRenderers, while retaining
`upgrade.chartNameChangeStrategy: InPlaceUpdate` in every release. A plain revert
that removes this field could trigger an uninstall when changing back to the
legacy chart names. Preserve the working image digests in the rendered Pod specs
rather than relying on the mutable `v1` tag, and compare rollback rendering with
the pre-migration baseline before applying.

Continue using the `flux-system` source for rollback; do not resume the suspended
upstream GitRepository. Verify Ready conditions, resource UIDs, PVC bindings, and
application flows again after rollback. Database restores are not part of this
chart-only rollback.

## Updates after migration

The migration retained running image digests. Subsequent chart and image updates
are proposed separately by GitHub Actions and require manual review and merge.
See [Fluxer update operation](fluxer-auto-update.md) for the image lock, CI gates,
and application checks.
