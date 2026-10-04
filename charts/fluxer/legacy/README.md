# Legacy Fluxer charts

These charts are copied without modification from
[fluxerapp/fluxer](https://github.com/fluxerapp/fluxer) at commit
`0b6edf569086098ba5be58fc4990e5065a56d9ce`.
The upstream license is preserved in `LICENSE`.

Only the nine charts used by the current deployment (`admin`, `api`,
`app-proxy`, `gateway`, `media-proxy`, `messages`, `snowflakes`, `unfurl`,
and `users`) and their local library dependencies (`common` and `svc-common`)
are included. The relative dependency paths from the upstream chart layout
are preserved.

The HelmReleases use the existing `flux-system` GitRepository to read these
charts from this repository. This avoids cloning the large upstream Git
history for the pinned commit. The `fluxer-upstream` GitRepository is
suspended while these local copies are in use.

Dependency archives and lock files are generated with `helm dependency build`
when building a chart; they are not included in this copy. For example:

```sh
helm dependency build charts/fluxer/legacy/api
```

This is a recovery measure for the current chart layout. Migration to the
latest upstream chart layout will be handled in a separate PR.
