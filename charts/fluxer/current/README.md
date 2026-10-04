# Current Fluxer charts

The five charts in this directory are unmodified copies of
[fluxerapp/fluxer](https://github.com/fluxerapp/fluxer) at commit
`5799ef705dccb993560be1a31e9b129cf5772968`:
`fluxer-api`, `fluxer-web`, `fluxer-media-proxy`, `fluxer-gateway`, and `fluxer-svc`.
The upstream license is preserved in `LICENSE`. `UPSTREAM.json` records the
source commit and SHA256 of every copied file, including the license.

The nine existing HelmRelease names are retained. Each release enables only
its own workloads, using these local charts through the existing `flux-system`
GitRepository. This avoids the large full-history clone required by an upstream
GitRepository with a pinned commit. The previous charts remain in `../legacy`
for rollback.

Application images are pinned to the digests of the existing Ready Pods. This
migration changes chart layout without updating application binaries. API's
NATS startup wrapper, legacy service selectors, and existing PDB resource names
are preserved with HelmRelease postRenderers. Pod chart labels use the base
chart version so unrelated infra commits do not cause Pod restarts when Flux
adds a Git revision suffix to the chart artifact version.

## Validation

```sh
uv run --with pyyaml==6.0.3 python tests/scripts/fluxer-chart-test.py
```

The test renders all nine releases and applies every postRenderer in order.
It checks the pre-migration resource inventory, namespaces, selectors,
StatefulSet immutable fields, image digests, and normalized runtime/network
specification hashes. It also renders a revision-suffixed Chart.Version and
requires identical Pod templates. CI runs kubeconform against all rendered
resources.

The deployment contract was recorded from the existing Helm release manifests
and Ready Pod image digests before migration. Contract updates require an
explicitly reviewed behavior change; do not regenerate hashes blindly from a
failing candidate render.

## Updating the vendor

Fetch the desired upstream commit archive and replace only these five chart
directories and `LICENSE` with unmodified upstream files. Update the commit and
file hashes in `UPSTREAM.json`, review the upstream changes and migration
contracts, then run the validation above. Keep upstream changes separate from
application-image updates. Renovate does not advance this vendored source.

See `docs/fluxer-chart-migration.md` for rollout and rollback checks.
