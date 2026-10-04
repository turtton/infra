# Current Fluxer charts

The five charts in this directory are unmodified copies of
[fluxerapp/fluxer](https://github.com/fluxerapp/fluxer) at the commit recorded in
`UPSTREAM.json`:
`fluxer-api`, `fluxer-web`, `fluxer-media-proxy`, `fluxer-gateway`, and `fluxer-svc`.
The upstream license is preserved in `LICENSE`. `UPSTREAM.json` records the
source commit and SHA256 of every copied file, including the license.

The nine existing HelmRelease names are retained. Each release enables only
its own workloads, using these local charts through the existing `flux-system`
GitRepository. This avoids the large full-history clone required by an upstream
GitRepository with a pinned commit. The previous charts remain in `../legacy`
for rollback.

Application images are pinned by `clusters/main/apps/fluxer/images.lock.json`.
The initial lock preserves the digests of the Ready Pods before migration. API's
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
specification hashes, Helm ownership metadata, and the image lock. It also renders a revision-suffixed Chart.Version and
requires identical Pod templates. CI runs kubeconform against all rendered
resources.

The deployment contract was recorded from the existing Helm release manifests
and Ready Pod image digests before migration. Contract updates require an
explicitly reviewed behavior change; do not regenerate hashes blindly from a
failing candidate render.

## Updating the vendor

GitHub Actions checks upstream daily and creates separate chart and image PRs.
`scripts/update-fluxer.py` retrieves only these five charts and the license,
verifies immutable source bytes, and updates `UPSTREAM.json`. It separately
resolves `v1` images to immutable Linux/amd64 digests and updates the image lock.
There is no automatic merge. Incompatible chart candidates become draft PRs;
the deployment contract must not be rewritten just to make a candidate pass.
Renovate continues to handle other infrastructure dependencies.

See `docs/fluxer-auto-update.md` for operation and review checks, and
`docs/fluxer-chart-migration.md` for migration history and rollback checks.
