#!/usr/bin/env python3
"""Offline checks for resource limits, slot races and sensitive failure paths.

Run: uv run python tests/scripts/test-ci-runners-trial.py
"""

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("ci_trial", ROOT / "scripts/ci-runners-trial.py")
trial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trial)


def options(*args):
    return trial.parser().parse_args(["create", "--size", "medium", *args])


def fixture_job(slot="ci-trial-medium-0", **extra):
    job = {"kind": "Job", "metadata": {"name": slot, "uid": "test-uid", "labels": trial.MANAGED}}
    job.update(extra)
    return job


def gib(quantity):
    if quantity.endswith("Gi"):
        return float(quantity[:-2])
    if quantity.endswith("Mi"):
        return float(quantity[:-2]) / 1024
    raise AssertionError(quantity)


class ManifestTests(unittest.TestCase):
    def job(self, *args):
        opts = options(*args)
        trial.validate_options(opts)
        return trial.make_job(opts, f"ci-trial-{opts.size}-0", "lepi-ci-test", "jit-test")

    def test_runtime_resource_envelopes_include_docker(self):
        for flags, cpu, memory in [(["--size", "small"], 2, 4), ([], 4, 8),
                                   (["--cpu-override", "2", "--label", "lepi-ci-baseline"], 2, 8),
                                   (["--docker"], 4, 8)]:
            with self.subTest(flags=flags):
                pod = self.job(*flags)["spec"]["template"]["spec"]
                running = pod["containers"] + [c for c in pod["initContainers"] if c.get("restartPolicy") == "Always"]
                for allocation in ("requests", "limits"):
                    self.assertEqual(sum(float(c["resources"][allocation]["cpu"]) for c in running), cpu)
                    self.assertEqual(sum(gib(c["resources"][allocation]["memory"]) for c in running), memory)
                self.assertEqual(sum(gib(c["resources"]["limits"]["ephemeral-storage"]) for c in running), 2)

    def test_baseline_label_does_not_match_medium_jobs(self):
        opts = options("--cpu-override", "2", "--label", "lepi-ci-baseline")
        self.assertIn("lepi-ci-baseline", trial.runner_labels(opts))
        self.assertNotIn("lepi-ci-medium", trial.runner_labels(opts))
        for flags in [("--cpu-override", "2"), ("--size", "small", "--cpu-override", "2"),
                      ("--cpu-override", "2", "--label", "lepi-ci-baseline", "--docker"),
                      ("--size", "small", "--docker")]:
            with self.subTest(flags=flags), self.assertRaises(trial.TrialError):
                trial.validate_options(options(*flags))

    def test_all_bulk_data_uses_the_same_ephemeral_pvc(self):
        pod = self.job("--docker")["spec"]["template"]["spec"]
        data = next(v for v in pod["volumes"] if v["name"] == "data")
        claim = data["ephemeral"]["volumeClaimTemplate"]["spec"]
        self.assertEqual(claim["storageClassName"], "longhorn-single")
        self.assertEqual(claim["resources"]["requests"]["storage"], "40Gi")
        mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
        for path in ("/home/runner/_work", "/home/runner/.cache", "/opt/hostedtoolcache", "/nix", "/tmp"):
            self.assertEqual(mounts[path]["name"], "data")
            self.assertIn("subPath", mounts[path])
        docker = next(c for c in pod["initContainers"] if c["name"] == "dind")
        self.assertEqual(next(m for m in docker["volumeMounts"] if m["mountPath"] == "/var/lib/docker")["name"], "data")

    def test_jobs_are_bounded_and_do_not_mount_host_or_kubernetes_credentials(self):
        job = self.job("--docker")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 4200)
        self.assertEqual(job["spec"]["ttlSecondsAfterFinished"], 300)
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        for unsafe in ("hostNetwork", "hostPID", "hostIPC"):
            self.assertFalse(pod.get(unsafe, False))
        self.assertTrue(all("hostPath" not in v for v in pod["volumes"]))
        self.assertNotIn("privileged", pod["containers"][0]["securityContext"])
        allowed = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
        self.assertEqual(set(allowed), {"toliworker-1", "toliworker-2", "toliworker-3"})

    def test_secret_is_owned_by_exact_job_uid(self):
        secret = trial.make_secret(fixture_job(), "jit-new", "sensitive-jit")
        self.assertEqual(secret["metadata"]["ownerReferences"][0]["uid"], "test-uid")
        self.assertEqual(secret["stringData"], {"config": "sensitive-jit"})

    def test_render_is_offline_and_contains_no_jit_payload(self):
        output = io.StringIO()
        with patch.object(trial, "command", side_effect=AssertionError("no network")), contextlib.redirect_stdout(output):
            self.assertEqual(trial.main(["render", "--size", "small"]), 0)
        job = json.loads(output.getvalue())
        self.assertEqual(job["kind"], "Job")
        self.assertNotIn("stringData", output.getvalue())


class LifecycleTests(unittest.TestCase):
    def test_terminating_pod_reserves_slot_after_its_job_disappears(self):
        pod = {"kind": "Pod", "metadata": {"name": "old-pod", "deletionTimestamp": "now",
                                           "labels": {trial.PREFIX + "slot": "ci-trial-small-0"}},
               "status": {"phase": "Running"}}
        self.assertEqual(trial.available_slots("small", [pod]), ["ci-trial-small-1"])
        pod["metadata"].pop("deletionTimestamp")
        pod["status"]["phase"] = "Succeeded"
        self.assertEqual(len(trial.available_slots("small", [pod])), 2)

    def test_two_slots_per_size_and_four_global(self):
        medium_jobs = [fixture_job(f"ci-trial-medium-{i}") for i in range(2)]
        self.assertEqual(trial.available_slots("medium", medium_jobs), [])
        all_jobs = medium_jobs + [fixture_job(f"ci-trial-small-{i}") for i in range(2)]
        self.assertEqual(trial.available_slots("small", all_jobs), [])

    def test_atomic_job_conflict_does_not_register_an_extra_runner(self):
        kube_calls = []
        def fake_kube(args, payload=None, **kwargs):
            kube_calls.append((args, payload))
            if args[:2] == ["create", "-f"] and payload["kind"] == "Job":
                if payload["metadata"]["name"].endswith("-0"):
                    raise trial.CommandError("create", 1, "AlreadyExists")
                payload["metadata"]["uid"] = "reserved-uid"
                return payload
            return {}
        with patch.object(trial, "check_namespace"), patch.object(trial, "snapshot", return_value=[]), \
             patch.object(trial, "kube", side_effect=fake_kube), \
             patch.object(trial, "github", return_value={"runner": {"id": 17}, "encoded_jit_config": "secret"}) as gh:
            result = trial.create_runner(options())
        self.assertEqual(result["job"], "ci-trial-medium-1")
        gh.assert_called_once()
        secret = next(payload for _, payload in kube_calls if payload and payload.get("kind") == "Secret")
        self.assertEqual(secret["metadata"]["ownerReferences"][0]["uid"], "reserved-uid")

    def test_secret_failure_removes_pod_before_unregistering_runner(self):
        calls = []
        def fake_kube(args, payload=None, **kwargs):
            if payload and payload.get("kind") == "Job":
                payload["metadata"]["uid"] = "uid"
                return payload
            if payload and payload.get("kind") == "Secret":
                raise trial.CommandError("Secret", 1, "echoed-sensitive-payload")
            return {}
        with patch.object(trial, "check_namespace"), patch.object(trial, "snapshot", return_value=[]), \
             patch.object(trial, "kube", side_effect=fake_kube), \
             patch.object(trial, "github", return_value={"runner": {"id": 17}, "encoded_jit_config": "secret"}), \
             patch.object(trial, "delete_job", side_effect=lambda name: calls.append("job")), \
             patch.object(trial, "delete_runner", side_effect=lambda repo, rid: calls.append("runner")):
            with self.assertRaises(trial.CommandError) as caught:
                trial.create_runner(options())
        self.assertEqual(calls, ["job", "runner"])
        self.assertNotIn("echoed-sensitive-payload", str(caught.exception))

    def test_cleanup_skips_active_jobs_unless_explicitly_cancelled(self):
        active = fixture_job()
        opts = trial.parser().parse_args(["cleanup"])
        with patch.object(trial, "snapshot", return_value=[active]), patch.object(trial, "delete_job") as delete:
            self.assertEqual(trial.cleanup(opts), {"deleted_jobs": []})
            delete.assert_not_called()
        opts.job = active["metadata"]["name"]
        with patch.object(trial, "snapshot", return_value=[active]), self.assertRaises(trial.TrialError):
            trial.cleanup(opts)

    def test_stderr_from_secret_bearing_command_never_reaches_user(self):
        proc = subprocess.CompletedProcess([], 1, "secret-stdout", "secret-stderr")
        with patch.object(trial.subprocess, "run", return_value=proc):
            with self.assertRaises(trial.CommandError) as caught:
                trial.command(["kubectl"], {"stringData": {"config": "secret"}}, "create JIT Secret")
        self.assertNotIn("secret-stdout", str(caught.exception))
        self.assertNotIn("secret-stderr", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
