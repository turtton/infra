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

kustomize build "${ATM10_DIR}" > "${BUILD_DIR}/atm10-rendered.yaml"
kustomize build "${MONITORING_DIR}" > "${BUILD_DIR}/monitoring-rendered.yaml"

DEPLOYMENT="${BUILD_DIR}/atm10-rendered.yaml"

if ! yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10")] | length == 1' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "ATM10 Deployment not rendered exactly once"
fi

if ! yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].image] | unique | .[0] == "itzg/minecraft-server:latest"' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "ATM10 image is not itzg/minecraft-server:latest"
fi

if yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].env[]? | select(.name == "CURSEFORGE_FILES" or .name == "CF_FILE_ID")] | length > 0' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "Deployment contains CURSEFORGE_FILES or CF_FILE_ID"
fi

METRICS_PORT_COUNT="$(yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].ports[]? | select(.name == "metrics" and .containerPort == 19565 and .protocol == "TCP")] | length' "${DEPLOYMENT}" 2>/dev/null || echo 0)"
if [ "${METRICS_PORT_COUNT}" != "1" ]; then
  fail "Deployment does not expose exactly one metrics port 19565/TCP (count=${METRICS_PORT_COUNT})"
fi

MC_PORT_COUNT="$(yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].ports[]? | select(.name == "minecraft" and .containerPort == 25565 and .protocol == "TCP")] | length' "${DEPLOYMENT}" 2>/dev/null || echo 0)"
if [ "${MC_PORT_COUNT}" != "1" ]; then
  fail "Deployment does not expose exactly one minecraft port 25565/TCP (count=${MC_PORT_COUNT})"
fi

for env_name in EULA TYPE CF_SLUG CF_API_KEY MEMORY OVERRIDE_SERVER_PROPERTIES DIFFICULTY MOTD ICON OPS; do
  if ! yq -e -s "[.[] | select(.kind == \"Deployment\" and .metadata.name == \"atm10\") | .spec.template.spec.containers[0].env[]? | select(.name == \"${env_name}\")] | length > 0" "${DEPLOYMENT}" >/dev/null 2>&1; then
    fail "Deployment is missing expected env var ${env_name}"
  fi
done

if ! yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].resources.requests.cpu] | unique | .[0] == 2' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "Deployment CPU request changed"
fi
if ! yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].resources.limits.cpu] | unique | .[0] == 4' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "Deployment CPU limit changed"
fi
if ! yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].resources.requests.memory] | unique | .[0] == "8Gi"' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "Deployment memory request changed"
fi
if ! yq -e -s '[.[] | select(.kind == "Deployment" and .metadata.name == "atm10") | .spec.template.spec.containers[0].resources.limits.memory] | unique | .[0] == "12Gi"' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "Deployment memory limit changed"
fi

for vol_name in data downloads; do
  if ! yq -e -s "[.[] | select(.kind == \"Deployment\" and .metadata.name == \"atm10\") | .spec.template.spec.volumes[]? | select(.name == \"${vol_name}\")] | length > 0" "${DEPLOYMENT}" >/dev/null 2>&1; then
    fail "Deployment is missing expected volume ${vol_name}"
  fi
done

SERVICE_FILE="${ATM10_DIR}/service.yaml"
if [ -f "${SERVICE_FILE}" ]; then
  SERVICE_PORTS="$(yq -e -s '.[0].spec.ports | length' "${SERVICE_FILE}" 2>/dev/null || echo 0)"
  if [ "${SERVICE_PORTS}" != "1" ]; then
    fail "ATM10 service exposes ${SERVICE_PORTS} ports; expected 1"
  fi
  if ! yq -e -s '.[0].spec.ports[0].port == 25565' "${SERVICE_FILE}" >/dev/null 2>&1; then
    fail "ATM10 service port changed from 25565"
  fi
fi

PM_COUNT="$(yq -e -s '[.[] | select(.kind == "PodMonitor" and .metadata.name == "atm10")] | length' "${DEPLOYMENT}" 2>/dev/null || echo 0)"
if [ "${PM_COUNT}" != "1" ]; then
  fail "ATM10 PodMonitor not rendered (count=${PM_COUNT})"
fi

if ! yq -e -s '[.[] | select(.kind == "PodMonitor" and .metadata.name == "atm10") | .metadata.labels.release] | unique | .[0] == "kube-prometheus-stack"' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "ATM10 PodMonitor missing release label"
fi

if ! yq -e -s '[.[] | select(.kind == "PodMonitor" and .metadata.name == "atm10") | .spec.selector.matchLabels.app] | unique | .[0] == "atm10"' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "ATM10 PodMonitor selector mismatch"
fi

if ! yq -e -s '[.[] | select(.kind == "PodMonitor" and .metadata.name == "atm10") | .spec.podMetricsEndpoints[0]] | unique | .[0].port == "metrics" and .[0].path == "/metrics" and .[0].interval == "30s" and .[0].scrapeTimeout == "10s"' "${DEPLOYMENT}" >/dev/null 2>&1; then
  fail "ATM10 PodMonitor endpoint spec mismatch"
fi

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

DASHBOARD_FILE="${MONITORING_DIR}/dashboards/atm10.json"
if [ -f "${DASHBOARD_FILE}" ]; then
  if ! jq -e '.uid == "atm10-overview" and .title == "ATM10" and .schemaVersion == 39 and .refresh == "30s"' "${DASHBOARD_FILE}" >/dev/null 2>&1; then
    fail "Dashboard metadata mismatch"
  fi

  PANEL_COUNT="$(jq '.panels | length' "${DASHBOARD_FILE}" 2>/dev/null || echo 0)"
  UNIQUE_PANEL_COUNT="$(jq '[.panels[].id] | unique | length' "${DASHBOARD_FILE}" 2>/dev/null || echo 0)"
  if [ "${PANEL_COUNT}" != "${UNIQUE_PANEL_COUNT}" ]; then
    fail "Dashboard panel IDs are not unique"
  fi

  for title in "Server Ready" "Exporter" "Online Players" "Estimated TPS" "Average MSPT" "PVC Usage" "ATM10 Logs"; do
    if ! jq -e --arg t "${title}" '[.panels[].title] | index($t) != null' "${DASHBOARD_FILE}" >/dev/null 2>&1; then
      fail "Dashboard missing panel: ${title}"
    fi
  done

  for metric in mc_player_list mc_server_tick_seconds mc_dimension_tick_seconds mc_dimension_chunks_loaded mc_entities_total jvm_memory_bytes_used process_cpu_seconds_total kube_pod_status_ready container_memory_working_set_bytes kubelet_volume_stats_used_bytes; do
    if ! grep -qF "${metric}" "${DASHBOARD_FILE}"; then
      fail "Dashboard missing metric reference: ${metric}"
    fi
  done

  if grep -q 'or vector(0)' "${DASHBOARD_FILE}"; then
    fail "Dashboard uses unconditional or vector(0)"
  fi
  if grep -qE 'rate\(\s*mc_entities_total' "${DASHBOARD_FILE}" || grep -qE 'increase\(\s*mc_entities_total' "${DASHBOARD_FILE}"; then
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
