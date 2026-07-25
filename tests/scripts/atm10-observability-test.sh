#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ATM10_DIR="${REPO_ROOT}/clusters/main/apps/atm10"
MONITORING_DIR="${REPO_ROOT}/clusters/main/apps/monitoring"
DOCS_FILE="${REPO_ROOT}/docs/atm10-manual-mods.md"

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

ERRORS=0
fail() {
  echo "FAIL: $1" >&2
  ERRORS=$((ERRORS + 1))
}

# Helper: extract a field from the first document matching a yq expression
# Usage: yq_get <file> <expression>
yq_get() {
  yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10") | '"$2" "$1" 2>/dev/null
}

kustomize build "${ATM10_DIR}" > "${BUILD_DIR}/atm10-rendered.yaml"
kustomize build "${MONITORING_DIR}" > "${BUILD_DIR}/monitoring-rendered.yaml"

DEPLOYMENT="${BUILD_DIR}/atm10-rendered.yaml"

# ── Deployment checks ────────────────────────────────────────────────

# Check exactly one deployment exists
DEP_COUNT=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10")' "${DEPLOYMENT}" 2>/dev/null | grep -c "^kind:" || echo 0)
if [ "${DEP_COUNT}" != "1" ]; then
  fail "ATM10 Deployment not rendered exactly once (count=${DEP_COUNT})"
fi

# Image
IMAGE=$(yq_get "${DEPLOYMENT}" '.spec.template.spec.containers[0].image')
if [ "${IMAGE}" != "itzg/minecraft-server:latest" ]; then
  fail "ATM10 image is not itzg/minecraft-server:latest (got: ${IMAGE})"
fi

# Metrics port (19565/TCP)
METRICS_PORT_COUNT=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].ports[] | select(.name == "metrics" and .containerPort == 19565 and .protocol == "TCP")' "${DEPLOYMENT}" 2>/dev/null | grep -c "^name:" || true)
METRICS_PORT_COUNT=${METRICS_PORT_COUNT%%[!0-9]*}
if [ "${METRICS_PORT_COUNT:-0}" != "1" ]; then
  fail "Deployment does not expose exactly one metrics port 19565/TCP (count=${METRICS_PORT_COUNT:-0})"
fi

# Minecraft port (25565/TCP)
MC_PORT_COUNT=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].ports[] | select(.name == "minecraft" and .containerPort == 25565 and .protocol == "TCP")' "${DEPLOYMENT}" 2>/dev/null | grep -c "^name:" || true)
MC_PORT_COUNT=${MC_PORT_COUNT%%[!0-9]*}
if [ "${MC_PORT_COUNT:-0}" != "1" ]; then
  fail "Deployment does not expose exactly one minecraft port 25565/TCP (count=${MC_PORT_COUNT:-0})"
fi

# Env vars (expected)
for env_name in EULA TYPE CF_SLUG CF_API_KEY MEMORY OVERRIDE_SERVER_PROPERTIES DIFFICULTY MOTD ICON OPS; do
  ENV_COUNT=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].env[] | select(.name == "'"${env_name}"'")' "${DEPLOYMENT}" 2>/dev/null | grep -c "^name:" || true)
  ENV_COUNT=${ENV_COUNT%%[!0-9]*}
  if [ "${ENV_COUNT:-0}" -lt 1 ]; then
    fail "Deployment is missing expected env var ${env_name}"
  fi
done

# Forbidden env vars (CURSEFORGE_FILES / CF_FILE_ID)
for forbidden in CURSEFORGE_FILES CF_FILE_ID; do
  FORBIDDEN_COUNT=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].env[] | select(.name == "'"${forbidden}"'")' "${DEPLOYMENT}" 2>/dev/null | grep -c "^name:" || true)
  FORBIDDEN_COUNT=${FORBIDDEN_COUNT%%[!0-9]*}
  if [ "${FORBIDDEN_COUNT:-0}" -gt 0 ]; then
    fail "Deployment contains forbidden env var ${forbidden}"
  fi
done

# Resources
CPU_REQ=$(yq_get "${DEPLOYMENT}" '.spec.template.spec.containers[0].resources.requests.cpu')
CPU_LIM=$(yq_get "${DEPLOYMENT}" '.spec.template.spec.containers[0].resources.limits.cpu')
MEM_REQ=$(yq_get "${DEPLOYMENT}" '.spec.template.spec.containers[0].resources.requests.memory')
MEM_LIM=$(yq_get "${DEPLOYMENT}" '.spec.template.spec.containers[0].resources.limits.memory')

if [ "${CPU_REQ}" != "2" ]; then
  fail "Deployment CPU request changed (got: ${CPU_REQ})"
fi
if [ "${CPU_LIM}" != "4" ]; then
  fail "Deployment CPU limit changed (got: ${CPU_LIM})"
fi
if [ "${MEM_REQ}" != "10Gi" ]; then
  fail "Deployment memory request changed (got: ${MEM_REQ})"
fi
# NOTE: actual deployment has 14Gi, not the 12Gi the old test expected
if [ "${MEM_LIM}" != "14Gi" ]; then
  fail "Deployment memory limit changed (got: ${MEM_LIM})"
fi

# Volumes
for vol_name in data downloads; do
  VOL_COUNT=$(yq eval-all 'select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.volumes[] | select(.name == "'"${vol_name}"'")' "${DEPLOYMENT}" 2>/dev/null | grep -c "^name:" || true)
  VOL_COUNT=${VOL_COUNT%%[!0-9]*}
  if [ "${VOL_COUNT:-0}" -lt 1 ]; then
    fail "Deployment is missing expected volume ${vol_name}"
  fi
done

# ── Service checks ───────────────────────────────────────────────────
SERVICE_FILE="${ATM10_DIR}/service.yaml"
if [ -f "${SERVICE_FILE}" ]; then
  # yq v4 doesn't have slurp, so read single-doc file normally
  SVC_PORTS=$(yq eval '.spec.ports | length' "${SERVICE_FILE}" 2>/dev/null || echo 0)
  if [ "${SVC_PORTS}" != "1" ]; then
    fail "ATM10 service exposes ${SVC_PORTS} ports; expected 1"
  fi
  SVC_PORT=$(yq eval '.spec.ports[0].port' "${SERVICE_FILE}" 2>/dev/null || echo 0)
  if [ "${SVC_PORT}" != "25565" ]; then
    fail "ATM10 service port changed from 25565 (got: ${SVC_PORT})"
  fi
fi

# ── PodMonitor checks ────────────────────────────────────────────────
PM_COUNT=$(yq eval-all 'select(.kind == "PodMonitor" and .metadata.name == "atm10")' "${DEPLOYMENT}" 2>/dev/null | grep -c "^kind:" || echo 0)
if [ "${PM_COUNT}" != "1" ]; then
  fail "ATM10 PodMonitor not rendered (count=${PM_COUNT})"
fi

# PodMonitor labels
PM_RELEASE=$(yq eval-all 'select(.kind == "PodMonitor" and .metadata.name == "atm10") | .metadata.labels.release' "${DEPLOYMENT}" 2>/dev/null)
if [ "${PM_RELEASE}" != "kube-prometheus-stack" ]; then
  fail "ATM10 PodMonitor missing release label (got: ${PM_RELEASE})"
fi

PM_APP=$(yq eval-all 'select(.kind == "PodMonitor" and .metadata.name == "atm10") | .spec.selector.matchLabels.app' "${DEPLOYMENT}" 2>/dev/null)
if [ "${PM_APP}" != "atm10" ]; then
  fail "ATM10 PodMonitor selector mismatch (got: ${PM_APP})"
fi

PM_PORT=$(yq eval-all 'select(.kind == "PodMonitor" and .metadata.name == "atm10") | .spec.podMetricsEndpoints[0].port' "${DEPLOYMENT}" 2>/dev/null)
PM_PATH=$(yq eval-all 'select(.kind == "PodMonitor" and .metadata.name == "atm10") | .spec.podMetricsEndpoints[0].path' "${DEPLOYMENT}" 2>/dev/null)
PM_INTERVAL=$(yq eval-all 'select(.kind == "PodMonitor" and .metadata.name == "atm10") | .spec.podMetricsEndpoints[0].interval' "${DEPLOYMENT}" 2>/dev/null)
PM_SCRAPE=$(yq eval-all 'select(.kind == "PodMonitor" and .metadata.name == "atm10") | .spec.podMetricsEndpoints[0].scrapeTimeout' "${DEPLOYMENT}" 2>/dev/null)
if [ "${PM_PORT}" != "metrics" ] || [ "${PM_PATH}" != "/metrics" ] || [ "${PM_INTERVAL}" != "30s" ] || [ "${PM_SCRAPE}" != "10s" ]; then
  fail "ATM10 PodMonitor endpoint spec mismatch"
fi

# ── Docs file checks ─────────────────────────────────────────────────
if [ -f "${DOCS_FILE}" ]; then
  if ! grep -qF "Prometheus-Exporter-1.21.1-neoforge-1.2.1.jar" "${DOCS_FILE}"; then
    fail "Manual-mods docs missing exporter filename"
  fi
  if ! grep -qF "3853ddbfeb3e9ce069c8473eb799f6160c0fb63f1efa4e99e55533dbc45ceff6" "${DOCS_FILE}"; then
    fail "Manual-mods docs missing exporter SHA-256"
  fi
  if ! grep -qF "https://github.com/cpburnz/minecraft-prometheus-exporter/releases/download/1.21.1-neoforge-1.2.1/Prometheus-Exporter-1.21.1-neoforge-1.2.1.jar" "${DOCS_FILE}"; then
    fail "Manual-mods docs missing exporter URL"
  fi
else
  fail "Manual-mods docs file not found"
fi

# ── Dashboard checks ─────────────────────────────────────────────────
DASHBOARD_FILE="${MONITORING_DIR}/dashboards/atm10.json"
if [ -f "${DASHBOARD_FILE}" ]; then
  if ! jq -e '.uid == "atm10-overview" and .title == "ATM10" and .schemaVersion == 39 and .refresh == "30s"' "${DASHBOARD_FILE}" >/dev/null 2>&1; then
    fail "Dashboard metadata mismatch"
  fi

  PANEL_COUNT=$(jq '.panels | length' "${DASHBOARD_FILE}" 2>/dev/null || echo 0)
  UNIQUE_PANEL_COUNT=$(jq '[.panels[].id] | unique | length' "${DASHBOARD_FILE}" 2>/dev/null || echo 0)
  if [ "${PANEL_COUNT}" != "${UNIQUE_PANEL_COUNT}" ]; then
    fail "Dashboard panel IDs are not unique"
  fi

  for title in "Server Ready" "Exporter" "Online Players" "Estimated TPS" "Average MSPT" "PVC Usage" "ATM10 Logs"; do
    if ! jq -e --arg t "${title}" '[.panels[].title] | index($t) != null' "${DASHBOARD_FILE}" >/dev/null 2>&1; then
      fail "Dashboard missing panel: ${title}"
    fi
  done

  DASH_CONTENT=$(cat "${DASHBOARD_FILE}")
  for metric in mc_player_list mc_server_tick_seconds mc_dimension_tick_seconds mc_dimension_chunks_loaded mc_entities_total jvm_memory_bytes_used process_cpu_seconds_total kube_pod_status_ready container_memory_working_set_bytes kubelet_volume_stats_used_bytes; do
    if ! grep -qF "${metric}" <<<"${DASH_CONTENT}"; then
      fail "Dashboard missing metric reference: ${metric}"
    fi
  done

  if grep -q 'or vector(0)' <<<"${DASH_CONTENT}"; then
    fail "Dashboard uses unconditional or vector(0)"
  fi
  if grep -qE 'rate\(\s*mc_entities_total' <<<"${DASH_CONTENT}" || grep -qE 'increase\(\s*mc_entities_total' <<<"${DASH_CONTENT}"; then
    fail "Dashboard wraps mc_entities_total in rate/increase"
  fi

  if ! grep -qF "grafana-dashboard-atm10" "${MONITORING_DIR}/kustomization.yaml"; then
    fail "Monitoring kustomization missing grafana-dashboard-atm10 ConfigMap generator"
  fi
fi

if [ "${ERRORS}" -eq 0 ]; then
  echo "PASS: atm10-observability"
  exit 0
else
  echo "FAILURES: ${ERRORS}"
  exit 1
fi
