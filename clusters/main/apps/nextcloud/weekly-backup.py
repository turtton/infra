#!/usr/bin/env python3
"""Coordinate a Nextcloud file/DB snapshot pair, then upload it to Longhorn R2.

A short-lived keeper Pod holds a read-only file PVC mount during snapshots.
It is deleted and the volume detached before Nextcloud restarts. PostgreSQL
stays online: PGDATA and WAL must be on its single PVC so crash recovery can
replay WAL. The only database writer, Nextcloud (including cron), is stopped
while both snapshots are captured. No credentials leave the application pods.
"""

import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

NS = "nextcloud"
LH = "longhorn-system"
DEPLOYMENT = "nextcloud"
DB_CLUSTER = "nextcloud-db"
FILE_PVC = "nextcloud-nextcloud"
STATE_NAME = "nextcloud-weekly-backup-state"
POLICY = "nextcloud-weekly"
POLICY_LABEL = "backup.nextcloud.turtton.net/policy"
PAIR_LABEL = "backup.nextcloud.turtton.net/pair"
PART_LABEL = "backup.nextcloud.turtton.net/part"
APP_SELECTOR = "app.kubernetes.io/name=nextcloud,app.kubernetes.io/instance=nextcloud,app.kubernetes.io/component=app"
POD_NAME = os.environ.get("POD_NAME", "")
POD_UID = os.environ.get("POD_UID", "")
QUIESCE_TIMEOUT = 600
BACKUP_TIMEOUT = 21600
RETAIN = 4
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
KEEPER_IMAGE = "docker.io/alpine/k8s:1.35.3@sha256:097aa60cbef561146757c7494468e9d7b04d843597ad1a1515ed09d0708c8014"


def log(message):
    print(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {message}", flush=True)


def kubectl(*args, namespace=NS, body=None, timeout=75):
    # kubectl does not discover the Pod's ServiceAccount credentials by itself.
    # Reopen the projected token on EVERY invocation so multi-hour uploads also
    # work after kubelet rotates the token. Keep credentials out of argv/logs.
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise RuntimeError("KUBERNETES_SERVICE_HOST is missing; run this script inside its backup Pod")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        token = (SERVICE_ACCOUNT_DIR / "token").read_text().strip()
    except OSError:
        raise RuntimeError("Projected ServiceAccount token is unavailable") from None
    if not token:
        raise RuntimeError("Projected ServiceAccount token is empty")
    config = {"apiVersion": "v1", "kind": "Config", "current-context": "backup",
              "clusters": [{"name": "cluster", "cluster": {
                  "server": f"https://{host}:{port}",
                  "certificate-authority": str(SERVICE_ACCOUNT_DIR / "ca.crt")}}],
              "users": [{"name": "service-account", "user": {"token": token}}],
              "contexts": [{"name": "backup", "context": {
                  "cluster": "cluster", "user": "service-account", "namespace": namespace}}]}
    operation = f"kubectl {args[0]} {args[1] if len(args) > 1 else ''}"
    # NamedTemporaryFile is created mode 0600 and removed on success or error.
    # A private kubeconfig also avoids accidental use of an image's local config.
    with tempfile.NamedTemporaryFile(mode="w", prefix="nextcloud-kubeconfig-", suffix=".json", dir="/tmp") as credentials:
        json.dump(config, credentials)
        credentials.flush()
        command = ["kubectl", "--kubeconfig", credentials.name, "--cache-dir=/tmp/kube-cache",
                   "--request-timeout=60s", "-n", namespace, *args]
        try:
            result = subprocess.run(command, input=json.dumps(body) if body else None,
                                    text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # TimeoutExpired's default text includes command/output; never log it.
            raise RuntimeError(f"{operation} timed out after {timeout}s") from None
        if result.returncode:
            # Do not include arbitrary exec output: config files can contain secrets.
            raise RuntimeError(f"{operation} failed ({result.returncode})")
        return result.stdout


def get(resource, name=None, namespace=NS, selector=None):
    args = ["get", resource]
    if name:
        args += [name, "--ignore-not-found"]
    if selector:
        args += ["-l", selector]
    value = kubectl(*args, "-o", "json", namespace=namespace)
    return json.loads(value) if value.strip() else None


def create(obj, namespace=NS):
    return json.loads(kubectl("create", "-f", "-", "-o", "json", body=obj, namespace=namespace))


def patch(resource, name, patch_data, namespace=NS):
    return json.loads(kubectl("patch", resource, name, "--type=json", "-p",
                             json.dumps(patch_data), "-o", "json", namespace=namespace))


def delete(resource, name, namespace=NS):
    kubectl("delete", resource, name, "--ignore-not-found", "--wait=false", namespace=namespace)


def until(description, check, timeout=180, delay=3):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(delay)


def state():
    obj = get("configmap", STATE_NAME)
    return (obj, json.loads(obj["data"]["state"])) if obj else (None, None)


def set_state(obj, value):
    return patch("configmap", STATE_NAME, [
        {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
        {"op": "replace", "path": "/data/state", "value": json.dumps(value)},
    ])


def owned_state():
    obj, value = state()
    if not obj or value["ownerUID"] != POD_UID:
        raise RuntimeError("Backup ownership lost; aborting")
    return obj, value


def assert_quiesced():
    _, value = owned_state()
    if value["phase"] != "quiescing" or time.time() >= value["resumeAfter"]:
        raise RuntimeError("Quiesce deadline reached or recovery started")
    if get("pods", selector=APP_SELECTOR)["items"]:
        raise RuntimeError("Nextcloud restarted before the snapshot pair completed")
    keeper = get("pod", value["keeperPod"])
    if not keeper or keeper["metadata"].get("deletionTimestamp") or keeper.get("status", {}).get("phase") != "Running":
        raise RuntimeError("File volume keeper was lost during snapshots")


def app_pod(require_ready=False):
    pods = get("pods", selector=APP_SELECTOR)["items"]
    for pod in pods:
        if pod["metadata"].get("deletionTimestamp"):
            continue
        containers = pod.get("status", {}).get("containerStatuses", [])
        app = next((c for c in containers if c["name"] == "nextcloud"), {})
        pod_ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in pod.get("status", {}).get("conditions", []))
        if app.get("state", {}).get("running") and (not require_ready or (app.get("ready") and pod_ready)):
            return pod
    return None


def occ(pod, *args):
    return kubectl("exec", pod["metadata"]["name"], "-c", "nextcloud", "--",
                   "runuser", "-u", "www-data", "--", "php", "/var/www/html/occ", *args)


def keeper_manifest(obj, value, node):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": value["keeperPod"], "namespace": NS,
                         "labels": {POLICY_LABEL: POLICY, PAIR_LABEL: value["pair"]},
                         "ownerReferences": [{"apiVersion": "v1", "kind": "ConfigMap", "name": STATE_NAME,
                                              "uid": obj["metadata"]["uid"], "blockOwnerDeletion": False}]},
            "spec": {"nodeName": node, "restartPolicy": "Never", "automountServiceAccountToken": False,
                     "activeDeadlineSeconds": 1800, "terminationGracePeriodSeconds": 5,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "keeper", "image": KEEPER_IMAGE, "command": ["sleep", "1800"],
                                     "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                         "capabilities": {"drop": ["ALL"]}},
                                     "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                                   "limits": {"memory": "32Mi"}},
                                     "volumeMounts": [{"name": "files", "mountPath": "/nextcloud-data", "readOnly": True}]}],
                     "volumes": [{"name": "files", "persistentVolumeClaim": {"claimName": FILE_PVC}}]}}


def start_keeper(obj, value, node):
    create(keeper_manifest(obj, value, node))
    def ready():
        keeper = get("pod", value["keeperPod"])
        if keeper and keeper.get("status", {}).get("phase") in ("Failed", "Succeeded"):
            raise RuntimeError("File volume keeper terminated before use")
        return bool(keeper and any(c["type"] == "Ready" and c["status"] == "True"
                                    for c in keeper.get("status", {}).get("conditions", [])))
    until("Read-only file volume keeper did not become Ready", ready, timeout=300)
    log(f"Read-only file volume keeper ready on {node}")


def remove_keeper(value):
    keeper = get("pod", value["keeperPod"])
    if keeper:
        if keeper["metadata"].get("labels", {}).get(PAIR_LABEL) != value["pair"]:
            raise RuntimeError("Keeper Pod ownership changed; refusing deletion")
        log("Removing file volume keeper before restarting Nextcloud")
        delete("pod", value["keeperPod"])
    until("File volume keeper did not terminate", lambda: not get("pod", value["keeperPod"]), timeout=180)


def file_volume_detached(value):
    volume = get("volumes.longhorn.io", value["fileVolume"], namespace=LH)
    if not volume or volume.get("status", {}).get("state") != "detached":
        return False
    attachments = get("volumeattachments.storage.k8s.io")["items"]
    return not any(a["spec"].get("source", {}).get("persistentVolumeName") == value["filePV"]
                   and a.get("status", {}).get("attached", False) for a in attachments)


def resume(obj, value):
    """Release the RWO keeper before restarting, including watchdog retries."""
    if value["phase"] == "uploading":
        return
    # A main process and the watchdog can meet at the quiesce timeout. Only one
    # live recovery actor may run the stop/detach/start sequence at a time.
    if value.get("recoveryOwnerUID") not in (None, POD_UID):
        actor = get("pod", value["recoveryOwnerPod"])
        if (actor and actor["metadata"]["uid"] == value["recoveryOwnerUID"]
                and actor.get("status", {}).get("phase") in ("Pending", "Running")
                and time.time() < value["recoveryDeadline"]):
            raise RuntimeError("Another process is already restoring Nextcloud")
    value.update({"phase": "resuming", "recoveryOwnerUID": POD_UID,
                  "recoveryOwnerPod": POD_NAME, "recoveryDeadline": int(time.time()) + 900})
    obj = set_state(obj, value)
    deploy = get("deployment", DEPLOYMENT)
    if deploy["metadata"]["uid"] != value["deploymentUID"]:
        raise RuntimeError("Deployment was replaced; refusing to change its scale")
    remove_keeper(value)
    if value["scaleDownAttempted"] and value.get("restartStage") != "start":
        # Also remove any Pending app Pod created by an external reconcile.
        # Otherwise it can request an attachment while the old mount is leaving.
        kubectl("scale", "deployment", DEPLOYMENT, "--replicas=0")
        until("Nextcloud pods did not stop for volume release",
              lambda: not get("pods", selector=APP_SELECTOR)["items"], timeout=180)
        until("File volume did not detach after keeper removal",
              lambda: file_volume_detached(value), timeout=180)
        log("File volume detached; Nextcloud can restart on any eligible node")
        latest, current = state()
        if not latest or current["ownerUID"] != value["ownerUID"]:
            raise RuntimeError("Recovery ownership changed")
        # Persist before scale-up. A retry can safely repeat scale=1 and must not
        # stop the newly started app while waiting for its maintenance reset.
        current["restartStage"] = "start"
        set_state(latest, current)
    kubectl("scale", "deployment", DEPLOYMENT, f"--replicas={value['replicas']}")
    pod = until("Nextcloud container did not return for maintenance reset", app_pod, timeout=240)
    # Ready is deliberately not required: maintenance mode can fail HTTP probes.
    if value["maintenanceOwned"]:
        occ(pod, "maintenance:mode", "--off")
        status = json.loads(occ(pod, "status", "--output=json"))
        if status.get("maintenance"):
            raise RuntimeError("Nextcloud maintenance mode is still enabled")
    until("Nextcloud did not become Ready after backup", lambda: app_pod(require_ready=True), timeout=180)
    latest, current = state()
    if not latest or current["ownerUID"] != value["ownerUID"]:
        raise RuntimeError("Recovery ownership changed")
    current["phase"] = "uploading"
    set_state(latest, current)
    log("Nextcloud resumed; snapshots can now upload without application downtime")


def owner_running(value):
    pod = get("pod", value["ownerPod"])
    return bool(pod and pod["metadata"]["uid"] == value["ownerUID"]
                and pod.get("status", {}).get("phase") in ("Pending", "Running"))


def recover():
    obj, value = state()
    if not obj:
        return
    running = owner_running(value)
    if value["phase"] != "uploading":
        if running and time.time() < value["resumeAfter"]:
            return
        log("Recovering an interrupted Nextcloud backup")
        resume(obj, value)
    if not running:
        # A terminating Pod is still treated as running until terminal/absent;
        # its finally block must not race the acquisition of a new lock.
        delete("configmap", STATE_NAME)
        log("Released interrupted backup ownership")


def volume_for_pvc(pvc_name):
    pvc = get("pvc", pvc_name)
    if not pvc or pvc.get("status", {}).get("phase") != "Bound":
        raise RuntimeError(f"PVC {pvc_name} is not Bound")
    pv = get("pv", pvc["spec"]["volumeName"])
    csi = pv["spec"].get("csi", {})
    if csi.get("driver") != "driver.longhorn.io" or not csi.get("volumeHandle"):
        raise RuntimeError(f"PVC {pvc_name} is not backed by Longhorn CSI")
    # A restored PV name need not equal the CSI volumeHandle.
    volume = get("volumes.longhorn.io", csi["volumeHandle"], namespace=LH)
    if not volume or volume["status"].get("state") != "attached" or volume["status"].get("robustness") != "healthy":
        raise RuntimeError(f"Volume for {pvc_name} is not attached and healthy")
    if volume["spec"].get("backupTargetName") != "default":
        raise RuntimeError(f"Volume for {pvc_name} does not use the default R2 target")
    volume["_boundPV"] = pvc["spec"]["volumeName"]
    return volume


def discover():
    cluster = get("clusters.postgresql.cnpg.io", DB_CLUSTER)
    if cluster["spec"].get("walStorage") or cluster["spec"].get("tablespaces"):
        raise RuntimeError("DB WAL/tablespaces must share the PGDATA PVC for an atomic snapshot")
    primary = cluster.get("status", {}).get("currentPrimary")
    if not primary or cluster["spec"].get("instances") != 1:
        raise RuntimeError("Expected one stable Nextcloud DB primary")
    db_pod = get("pod", primary)
    claims = [v["persistentVolumeClaim"]["claimName"] for v in db_pod["spec"]["volumes"]
              if "persistentVolumeClaim" in v]
    if len(claims) != 1 or not any(c["type"] == "Ready" and c["status"] == "True"
                                    for c in db_pod.get("status", {}).get("conditions", [])):
        raise RuntimeError("DB primary must be Ready with a single PVC containing PGDATA and WAL")
    return {"files": volume_for_pvc(FILE_PVC), "database": volume_for_pvc(claims[0])}, primary


def snapshot_name(pair, part):
    return f"nc-{pair}-{part}"


def backup_name(pair, part):
    return f"backup-nc-{pair}-{part}"


def labels(pair, part):
    return {POLICY_LABEL: POLICY, PAIR_LABEL: pair, PART_LABEL: part}


def create_snapshot(pair, part, volume):
    assert_quiesced()
    create({"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
            "metadata": {"name": snapshot_name(pair, part), "namespace": LH,
                         "labels": labels(pair, part)},
            "spec": {"volume": volume["metadata"]["name"], "createSnapshot": True,
                     "labels": {"nextcloud-pair": pair, "nextcloud-part": part}}}, namespace=LH)


def wait_snapshot(pair, part):
    def ready():
        assert_quiesced()
        snapshot = get("snapshots.longhorn.io", snapshot_name(pair, part), namespace=LH)
        status = snapshot.get("status", {})
        if status.get("error"):
            raise RuntimeError(f"{part} snapshot failed; inspect its Longhorn status")
        return status.get("readyToUse") and status.get("creationTime")
    until(f"{part} snapshot did not become ready", ready, timeout=120)


def create_backup(pair, part, volume):
    backup_labels = labels(pair, part)
    backup_labels.update({"backup-target": "default", "backup-volume": volume["metadata"]["name"]})
    stored_labels = {"nextcloud-policy": POLICY, "nextcloud-pair": pair, "nextcloud-part": part,
                     "longhorn.io/volume-access-mode": volume["spec"].get("accessMode", "rwo"),
                     "KubernetesStatus": json.dumps(volume["status"].get("kubernetesStatus", {}))}
    create({"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": {"name": backup_name(pair, part), "namespace": LH, "labels": backup_labels},
            "spec": {"snapshotName": snapshot_name(pair, part), "backupMode": "incremental",
                     "backupBlockSize": volume["spec"].get("backupBlockSize", "2097152"),
                     "labels": stored_labels}}, namespace=LH)


def wait_backups(pair):
    last_progress = None
    def complete():
        nonlocal last_progress
        progress = []
        for part in ("files", "database"):
            backup = get("backups.longhorn.io", backup_name(pair, part), namespace=LH)
            status = backup.get("status", {})
            if status.get("state") in ("Error", "Unknown") or status.get("error"):
                raise RuntimeError(f"{part} backup failed; inspect its Longhorn status")
            progress.append((part, status.get("state", "Pending"), status.get("progress", 0)))
            if status.get("state") == "Completed" and (status.get("progress") != 100 or not status.get("url")):
                raise RuntimeError(f"{part} backup completion lacks a verified remote URL/progress")
        if progress != last_progress:
            log(f"R2 upload progress: {progress}")
            last_progress = progress
        return all(p[1] == "Completed" and p[2] == 100 for p in progress)
    until("R2 backup pair exceeded upload deadline", complete, timeout=BACKUP_TIMEOUT, delay=20)


def prune():
    # Group from persisted spec labels as well as CR labels: Longhorn can recreate
    # Backup CRs from the R2 store, without our Kubernetes metadata labels.
    groups = {}
    for backup in get("backups.longhorn.io", namespace=LH)["items"]:
        stored = backup.get("spec", {}).get("labels", {})
        if stored.get("nextcloud-policy") != POLICY:
            continue
        pair = stored.get("nextcloud-pair")
        if pair:
            groups.setdefault(pair, []).append(backup)
    successful = []
    for pair, backups in groups.items():
        parts = {b["spec"]["labels"].get("nextcloud-part") for b in backups}
        if parts == {"files", "database"} and len(backups) == 2 and all(
            b.get("status", {}).get("state") == "Completed"
            and b["status"].get("progress") == 100 and b["status"].get("url") for b in backups
        ):
            successful.append(pair)
    successful.sort(reverse=True)
    for pair in successful[RETAIN:]:
        for backup in groups[pair]:
            delete("backups.longhorn.io", backup["metadata"]["name"], namespace=LH)
        log(f"Pruned completed R2 pair {pair}")
    # Retain the latest local pair as the incremental-backup base. Older local
    # snapshots are redundant once their remote pair is complete.
    for pair in successful[1:]:
        for part in ("files", "database"):
            delete("snapshots.longhorn.io", snapshot_name(pair, part), namespace=LH)
    # Failed/interrupted runs must not accumulate local snapshots forever. Only
    # discard an incomplete older pair once a newer complete pair exists; never
    # touch a pending or active upload, even if its coordinator disappeared.
    snapshots = get("snapshots.longhorn.io", namespace=LH, selector=f"{POLICY_LABEL}={POLICY}")["items"]
    orphan_pairs = {s["metadata"].get("labels", {}).get(PAIR_LABEL) for s in snapshots}
    for pair in sorted(p for p in orphan_pairs if p):
        if not successful or pair >= successful[0] or pair in successful:
            continue
        backups = groups.get(pair, [])
        if any(b.get("status", {}).get("state") not in ("Completed", "Error") for b in backups):
            continue
        for backup in backups:
            delete("backups.longhorn.io", backup["metadata"]["name"], namespace=LH)
        for part in ("files", "database"):
            delete("snapshots.longhorn.io", snapshot_name(pair, part), namespace=LH)
        log(f"Pruned incomplete older pair {pair} after a newer successful backup")
    log(f"Retention checked: keeping up to {RETAIN} successful R2 pairs")


def run():
    if not POD_NAME or not POD_UID:
        raise RuntimeError("Pod identity must be provided by the downward API")
    recover()
    if state()[0]:
        raise RuntimeError("Another backup owns the coordinator state")
    pod = app_pod(require_ready=True)
    deploy = get("deployment", DEPLOYMENT)
    if not pod or deploy["spec"].get("replicas") != 1:
        raise RuntimeError("Expected one running Nextcloud replica")
    if json.loads(occ(pod, "status", "--output=json")).get("maintenance"):
        raise RuntimeError("Nextcloud is already in maintenance mode; leaving it untouched")
    volumes, primary = discover()
    pair = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + POD_UID[:8]
    value = {"ownerUID": POD_UID, "ownerPod": POD_NAME, "phase": "preparing", "pair": pair,
             "resumeAfter": int(time.time()) + QUIESCE_TIMEOUT, "replicas": deploy["spec"]["replicas"],
             "deploymentUID": deploy["metadata"]["uid"], "maintenanceOwned": False,
             "scaleDownAttempted": False, "restartStage": "detach", "keeperPod": f"nc-backup-keeper-{pair}",
             "fileVolume": volumes["files"]["metadata"]["name"], "filePV": volumes["files"]["_boundPV"]}
    obj = create({"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": STATE_NAME, "namespace": NS, "labels": {POLICY_LABEL: POLICY}},
            "data": {"state": json.dumps(value)}})
    try:
        start_keeper(obj, value, pod["spec"]["nodeName"])
        current_pod = app_pod(require_ready=True)
        if not current_pod or current_pod["metadata"]["uid"] != pod["metadata"]["uid"]:
            raise RuntimeError("Nextcloud moved while the keeper was starting")
        obj, value = owned_state()
        value.update({"phase": "quiescing", "maintenanceOwned": True,
                      "resumeAfter": int(time.time()) + QUIESCE_TIMEOUT})
        set_state(obj, value)
        log(f"Starting snapshot pair {pair}")
        occ(pod, "maintenance:mode", "--on")
        # Give already-running web requests time to drain before pod termination.
        time.sleep(20)
        obj, value = owned_state()
        if value["phase"] != "quiescing":
            raise RuntimeError("Recovery started before scale-down")
        value["scaleDownAttempted"] = True
        set_state(obj, value)
        kubectl("scale", "deployment", DEPLOYMENT, "--replicas=0")
        until("Nextcloud pods did not stop", lambda: not get("pods", selector=APP_SELECTOR)["items"], timeout=180)
        # PostgreSQL remains running; flushing a checkpoint shortens restore WAL
        # replay. Both PGDATA and WAL are verified to be on this single volume.
        kubectl("exec", primary, "-c", "postgres", "--", "psql", "-U", "postgres", "-d", "postgres",
                "-v", "ON_ERROR_STOP=1", "-c", "CHECKPOINT;")
        kubectl("exec", value["keeperPod"], "-c", "keeper", "--", "sync")
        for part, volume in volumes.items():
            create_snapshot(pair, part, volume)
        for part in volumes:
            wait_snapshot(pair, part)
        assert_quiesced()
        resume(*owned_state())
        # Upload only the completed pair, after maintenance was successfully reset.
        for part, volume in volumes.items():
            create_backup(pair, part, volume)
        wait_backups(pair)
        prune()
        log(f"Completed weekly R2 backup pair {pair}")
    finally:
        obj, current = owned_state()
        if current["phase"] != "uploading":
            resume(obj, current)
        # A failed recovery intentionally leaves the durable state for the watchdog.
        delete("configmap", STATE_NAME)


def interrupted(signum, _frame):
    raise RuntimeError(f"Interrupted by signal {signum}; restoring application state")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("run", "recover"):
            raise RuntimeError("Expected run or recover")
        {"run": run, "recover": recover}[sys.argv[1]]()
    except Exception as error:
        log(f"ERROR: {error}")
        sys.exit(1)
