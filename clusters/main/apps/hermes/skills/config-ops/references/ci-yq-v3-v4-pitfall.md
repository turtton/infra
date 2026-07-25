# yq v3 → v4 CI Migration Notes (turtton/infra)

## Background

The `atm10-observability-test.sh` script (and any test using `yq -s`)
was written for yq v3, where `-s` meant **slurp mode** (read all YAML
documents into an array). CI installs `yq v4.44.5` where `-s` means
**`--split-exp`** (write each result to a separate file). This causes
every yq-based validation to fail silently.

## Symptoms

- All yq-based checks fail simultaneously with "cannot index array with 'kind'"
- Check-run annotation says "Process completed with exit code 1" at `.github` line 31
- Validation comment on the PR shows empty output
- `gh pr checks` shows the step failed but the log provides no useful info

## Fix Pattern

The core issue is `yq -s 'jq-style-expression' file`. Replace with:

### Deployment/Resource Selection (Multi-Doc)

```bash
# Before (yq v3):
yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10")] | length == 1' file.yaml

# After (yq v4):
COUNT=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10")' file.yaml 2>/dev/null | grep -c "^kind:" || true)
COUNT=${COUNT%%[!0-9]*}
if [ "${COUNT:-0}" != "1" ]; then fail; fi
```

### Field Extraction (Single Value)

```bash
# Before:
yq -e -s '[.[] | select(...)] | .spec.template.spec.containers[0].image] | unique | .[0] == "itzg/minecraft-server:latest"' file.yaml

# After:
IMAGE=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].image' file.yaml 2>/dev/null)
if [ "${IMAGE}" != "itzg/minecraft-server:latest" ]; then fail; fi
```

### Single-Document File (no eval-all needed)

```bash
# Before:
yq -e -s '.[0].spec.ports | length' service.yaml

# After:
yq eval '.spec.ports | length' service.yaml
```

### The `|| echo 0` Trap

`grep -c` already outputs `0` on stderr when there are no matches,
even though it exits with code 1. Adding `|| echo 0` produces
`"0\n0"` — two lines — which bash's `[ ... -gt 0 ]` rejects with
"integer expression expected".

```bash
# WRONG: produces "0\n0"
COUNT=$(yq eval-all '...' file 2>/dev/null | grep -c "^kind:" || echo 0)

# RIGHT: strips non-digits after clean fallback
COUNT=$(yq eval-all '...' file 2>/dev/null | grep -c "^kind:" || true)
COUNT=${COUNT%%[!0-9]*}
if [ "${COUNT:-0}" -gt 0 ]; then ...
```

## Related

- PR #95: fix/yq-v4-compat — full rewrite of atm10-observability-test.sh
- PR #92: renovate/dependencies-(minorpatch) — original trigger that exposed this
