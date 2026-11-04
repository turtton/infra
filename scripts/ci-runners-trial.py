#!/usr/bin/env python3
"""Create bounded, single-job GitHub runners without sharing the local gh token.

Run with uv run python scripts/ci-runners-trial.py --help.
Apply clusters/main/apps/ci-runners-trial separately before creating runners.
"""

import argparse
import json
import re
import subprocess
import sys
import uuid

NAMESPACE = "ci-runners-trial"
MANAGED = {"app.kubernetes.io/part-of": NAMESPACE}
SELECTOR = "app.kubernetes.io/part-of=ci-runners-trial"
PREFIX = "ci.turtton.net/"
RUNNER_IMAGE = "ghcr.io/actions/actions-runner:2.338.2@sha256:0ce3288e48b012a83685998f683a2ae0cbd284fa722c090dda0af5443215cf30"
DOCKER_IMAGE = "docker:28.5.2-dind@sha256:9a06753d2401cd049b34cd27dbbc3e0db717d4c1db7bc7f2efad1c187e00bf5a"
PROFILES = {"small": {"cpu": 2, "memory": 4}, "medium": {"cpu": 4, "memory": 8}}


class TrialError(Exception):
    pass


class CommandError(TrialError):
    def __init__(self, operation, returncode, stderr=""):
        # Tool errors can echo stdin, including a JIT Secret. Never expose raw output.
        super().__init__(f"{operation} failed (exit {returncode}); no command output was disclosed")
        self.stderr = stderr


def command(argv, payload=None, operation="command", empty=False):
    result = subprocess.run(
        argv,
        input=None if payload is None else json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise CommandError(operation, result.returncode, result.stderr)
    if empty or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise TrialError(f"{operation} returned invalid JSON; output suppressed") from None


def kube(args, payload=None, operation="Kubernetes API", empty=False):
    return command(
        ["kubectl", "--request-timeout=90s", "-n", NAMESPACE, *args],
        payload, operation, empty,
    )


def github(repo, path, method="GET", payload=None, empty=False):
    args = ["gh", "api", f"repos/{repo}/actions/{path}", "--method", method]
    if payload is not None:
        args += ["--input", "-"]
    return command(args, payload, f"GitHub {method} {path}", empty)


def resources(cpu, memory, ephemeral_request="512Mi", ephemeral_limit="2Gi"):
    return {
        "requests": {"cpu": str(cpu), "memory": memory, "ephemeral-storage": ephemeral_request},
        "limits": {"cpu": str(cpu), "memory": memory, "ephemeral-storage": ephemeral_limit},
    }


def validate_options(args):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise TrialError("--repo must be owner/repository")
    if args.cpu_override is not None and (args.size != "medium" or args.docker):
        raise TrialError("--cpu-override 2 requires medium without --docker")
    if args.docker and args.size != "medium":
        raise TrialError("--docker is supported only by medium")
    if args.cpu_override is not None and args.label is None:
        raise TrialError("a CPU comparison requires an explicit --label, such as lepi-ci-baseline")
    if args.label and not re.fullmatch(r"lepi-ci-[a-z0-9-]{1,45}", args.label):
        raise TrialError("--label must start with lepi-ci- and contain lowercase letters, digits or hyphens")
    if args.name and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,42}[a-z0-9]|[a-z0-9]", args.name):
        raise TrialError("--name must be a short lowercase DNS label")


def runner_labels(args):
    return ["self-hosted", "Linux", "X64", args.label or f"lepi-ci-{args.size}"]


def volume_mount(path, subpath):
    return {"name": "data", "mountPath": path, "subPath": subpath}


def make_job(args, slot, runner_name, secret_name):
    cpu = args.cpu_override or PROFILES[args.size]["cpu"]
    memory = PROFILES[args.size]["memory"]
    labels = {**MANAGED, PREFIX + "size": args.size, PREFIX + "slot": slot}
    runner = {
        "name": "runner",
        "image": RUNNER_IMAGE,
        "command": ["/bin/bash", "-euc"],
        "args": ['exec /home/runner/run.sh --jitconfig "$(cat /run/ci-jit/config)"'],
        "env": [
            {"name": "CI_RUNNER_CPUS", "value": str(cpu)},
            {"name": "CI_RUNNER_MEMORY_GIB", "value": str(memory)},
            {"name": "GRADLE_USER_HOME", "value": "/home/runner/_work/.gradle"},
            {"name": "RUNNER_TOOL_CACHE", "value": "/opt/hostedtoolcache"},
            {"name": "AGENT_TOOLSDIRECTORY", "value": "/opt/hostedtoolcache"},
            {"name": "ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT", "value": "1"},
        ],
        "securityContext": {"runAsUser": 1001, "runAsGroup": 1001},
        "resources": resources(cpu, f"{memory}Gi"),
        "volumeMounts": [
            volume_mount("/home/runner/_work", "work"),
            volume_mount("/home/runner/.cache", "cache"),
            volume_mount("/opt/hostedtoolcache", "toolcache"),
            volume_mount("/nix", "nix"),
            volume_mount("/tmp", "tmp"),
            {"name": "jit", "mountPath": "/run/ci-jit", "readOnly": True},
        ],
    }
    prepare = {
        "name": "prepare-data",
        "image": RUNNER_IMAGE,
        "command": ["/bin/bash", "-euc"],
        "args": [
            "mkdir -p /data/{work,cache,toolcache,nix,tmp,docker}; "
            "chown 1001:1001 /data/{work,cache,toolcache,tmp}; "
            "chmod 1777 /data/tmp; chmod 0755 /data/nix /data/docker"
        ],
        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
        "resources": resources(1, "128Mi", "64Mi", "128Mi"),
        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
    }
    pod = {
        "serviceAccountName": "ci-runner",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 30,
        "securityContext": {"supplementalGroups": [123], "seccompProfile": {"type": "RuntimeDefault"}},
        "nodeSelector": {"kubernetes.io/arch": "amd64", "kubernetes.io/os": "linux"},
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{
                "key": "kubernetes.io/hostname", "operator": "In",
                "values": ["toliworker-1", "toliworker-2", "toliworker-3"],
            }]}]
        }}},
        "topologySpreadConstraints": [{
            "maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
            "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": MANAGED},
        }],
        "initContainers": [prepare],
        "containers": [runner],
        "volumes": [
            {"name": "data", "ephemeral": {"volumeClaimTemplate": {
                "metadata": {"labels": labels},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "longhorn-single",
                         "resources": {"requests": {"storage": "40Gi"}}},
            }}},
            {"name": "jit", "secret": {"secretName": secret_name, "defaultMode": 0o444}},
        ],
    }
    if args.docker:
        runner["resources"] = resources(cpu - 1, f"{memory - 1}Gi", "448Mi", "1792Mi")
        runner["env"] += [
            {"name": "DOCKER_HOST", "value": "unix:///var/run/ci-docker/docker.sock"},
            {"name": "RUNNER_WAIT_FOR_DOCKER_IN_SECONDS", "value": "120"},
        ]
        runner["volumeMounts"].append({"name": "docker-sock", "mountPath": "/var/run/ci-docker"})
        pod["volumes"].append({"name": "docker-sock", "emptyDir": {"medium": "Memory", "sizeLimit": "8Mi"}})
        pod["initContainers"].append({
            "name": "dind", "image": DOCKER_IMAGE, "restartPolicy": "Always",
            "command": ["dockerd"],
            "args": ["--host=unix:///var/run/ci-docker/docker.sock", "--group=123", "--data-root=/var/lib/docker"],
            "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
            "resources": resources(1, "1Gi", "64Mi", "256Mi"),
            "startupProbe": {"exec": {"command": ["docker", "--host=unix:///var/run/ci-docker/docker.sock", "info"]},
                             "periodSeconds": 5, "failureThreshold": 36, "timeoutSeconds": 3},
            "volumeMounts": [
                volume_mount("/var/lib/docker", "docker"),
                volume_mount("/home/runner/_work", "work"),
                {"name": "docker-sock", "mountPath": "/var/run/ci-docker"},
            ],
        })
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": slot, "namespace": NAMESPACE, "labels": labels,
                     "annotations": {PREFIX + "repository": args.repo, PREFIX + "runner-name": runner_name,
                                     PREFIX + "runner-label": runner_labels(args)[-1]}},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 4200, "ttlSecondsAfterFinished": 300,
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    }


def make_secret(job, name, encoded_config):
    return {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": MANAGED,
                     "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job",
                                          "name": job["metadata"]["name"], "uid": job["metadata"]["uid"],
                                          "controller": True, "blockOwnerDeletion": True}]},
        "stringData": {"config": encoded_config},
    }


def completed(job):
    return any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
               for c in job.get("status", {}).get("conditions", []))


def snapshot():
    return kube(["get", "jobs,pods", "-l", SELECTOR, "-o", "json"])["items"]


def occupied_slots(items):
    occupied = set()
    for item in items:
        meta = item["metadata"]
        if item["kind"] == "Job":
            occupied.add(meta["name"])
        elif item["kind"] == "Pod" and (
            meta.get("deletionTimestamp") or item.get("status", {}).get("phase") not in ("Succeeded", "Failed")
        ):
            # A deleted Job's terminating Pod still occupies its slot.
            occupied.add(meta.get("labels", {}).get(PREFIX + "slot", meta["name"]))
    return occupied


def available_slots(size, items):
    occupied = occupied_slots(items)
    if len(occupied) >= 4:
        return []
    return [f"ci-trial-{size}-{i}" for i in range(2) if f"ci-trial-{size}-{i}" not in occupied]


def check_namespace():
    ns = kube(["get", "namespace", NAMESPACE, "-o", "json"])
    if ns["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != NAMESPACE:
        raise TrialError("namespace is not labelled for this trial; apply its standalone kustomization first")


def delete_runner(repo, runner_id):
    try:
        github(repo, f"runners/{runner_id}", method="DELETE", empty=True)
    except CommandError as exc:
        if "404" not in exc.stderr:
            raise


def delete_job(name):
    kube(["delete", "job", name, "--cascade=foreground", "--wait=true", "--timeout=90s", "--ignore-not-found"],
         operation=f"delete trial Job {name}", empty=True)


def create_runner(args):
    validate_options(args)
    check_namespace()
    nonce = uuid.uuid4().hex[:12]
    runner_name = "lepi-ci-" + (args.name or f"{args.size}-{nonce}")
    secret_name = f"jit-{nonce}"
    job = None
    for slot in available_slots(args.size, snapshot()):
        try:
            # The fixed Job name is the atomic slot reservation, before any GitHub registration.
            job = kube(["create", "-f", "-", "-o", "json"], make_job(args, slot, runner_name, secret_name),
                       operation="reserve runner slot")
            break
        except CommandError as exc:
            if "AlreadyExists" not in exc.stderr:
                raise
    if job is None:
        raise TrialError(f"no {args.size} slot available; maximum two per size / four total; clean up completed Jobs")
    runner_id = None
    try:
        response = github(args.repo, "runners/generate-jitconfig", "POST", {
            "name": runner_name, "runner_group_id": args.runner_group_id,
            "labels": runner_labels(args), "work_folder": "_work",
        })
        runner_id = int(response["runner"]["id"])
        patch = {"metadata": {"annotations": {PREFIX + "runner-id": str(runner_id)}}}
        kube(["patch", "job", job["metadata"]["name"], "--type=merge", "--patch", json.dumps(patch)],
             operation="record runner identity", empty=True)
        kube(["create", "-f", "-", "-o", "json"], make_secret(job, secret_name, response["encoded_jit_config"]),
             operation="create single-use JIT Secret")
    except (TrialError, KeyError, ValueError, subprocess.TimeoutExpired):
        # No Secret exists until the very last operation, so a failed registration cannot run a job.
        # Delete the Pod first in case Secret creation succeeded but the response was lost.
        delete_job(job["metadata"]["name"])
        if runner_id is not None:
            delete_runner(args.repo, runner_id)
        raise
    return {"job": job["metadata"]["name"], "runner": runner_name, "runner_id": runner_id,
            "size": args.size, "label": runner_labels(args)[-1], "docker": args.docker,
            "cpu": args.cpu_override or PROFILES[args.size]["cpu"], "memory_gib": PROFILES[args.size]["memory"]}


def status():
    return [{"kind": i["kind"], "name": i["metadata"]["name"],
             "size": i["metadata"].get("labels", {}).get(PREFIX + "size"),
             "node": i.get("spec", {}).get("nodeName"),
             "phase": i.get("status", {}).get("phase"),
             "completed": completed(i) if i["kind"] == "Job" else None,
             "runner": i["metadata"].get("annotations", {}).get(PREFIX + "runner-name")}
            for i in snapshot()]


def cleanup(args):
    jobs = [j for j in snapshot() if j["kind"] == "Job"]
    if args.job:
        jobs = [j for j in jobs if j["metadata"]["name"] == args.job]
        if not jobs:
            raise TrialError("no matching managed trial Job")
    removed = []
    for job in jobs:
        if not completed(job) and not args.cancel:
            if args.job:
                raise TrialError("Job is active; use --cancel only to stop this trial job")
            continue
        annotations = job["metadata"].get("annotations", {})
        repo = annotations.get(PREFIX + "repository")
        runner_id = annotations.get(PREFIX + "runner-id")
        name = job["metadata"]["name"]
        delete_job(name)
        if runner_id and repo:
            delete_runner(repo, int(runner_id))
        removed.append(name)
    return {"deleted_jobs": removed}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for action in ("render", "create"):
        sub = commands.add_parser(action, help="render a token-free Job" if action == "render" else "create one single-job runner")
        sub.add_argument("--size", choices=PROFILES, required=True)
        sub.add_argument("--repo", default="Lepinoid/lepinoid")
        sub.add_argument("--name", help="GitHub runner name suffix; Kubernetes slot names remain fixed")
        sub.add_argument("--label", help="custom lepi-ci-* workflow label (replaces the profile label)")
        sub.add_argument("--cpu-override", type=int, choices=[2], help="medium CPU comparison at identical 8 GiB memory")
        sub.add_argument("--docker", action="store_true", help="medium only: private privileged DinD sidecar within its 4 CPU / 8 GiB budget")
        sub.add_argument("--runner-group-id", type=int, default=1)
    commands.add_parser("status", help="show managed Jobs and Pods without secrets")
    clean = commands.add_parser("cleanup", help="delete completed trial Jobs and their ephemeral data")
    clean.add_argument("--job", choices=[f"ci-trial-{size}-{i}" for size in PROFILES for i in range(2)])
    clean.add_argument("--cancel", action="store_true", help="also stop selected active trial Jobs")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "render":
            validate_options(args)
            output = make_job(args, f"ci-trial-{args.size}-0", "lepi-ci-render-only", "jit-render-only")
        elif args.command == "create":
            output = create_runner(args)
        elif args.command == "status":
            output = status()
        else:
            output = cleanup(args)
        print(json.dumps(output, indent=2))
        return 0
    except (TrialError, subprocess.TimeoutExpired, OSError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            message = "external command timed out; inspect the trial Jobs and runner list before retrying"
        elif isinstance(exc, OSError):
            message = f"required local command unavailable: {exc.filename}"
        else:
            message = str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
