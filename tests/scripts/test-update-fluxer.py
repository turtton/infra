#!/usr/bin/env python3
"""Offline acquisition/transaction tests; run with uv run --with pyyaml==6.0.3 python this-file."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("update_fluxer", REPO / "scripts/update-fluxer.py")
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


def encoded(data):
    return json.dumps(data, sort_keys=True).encode()


def descriptor(raw, media_type):
    return {"mediaType": media_type, "digest": update.sha256(raw), "size": len(raw)}


class Remote:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def get(self, url, headers=None, limit=update.MAX_FILE):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError("Unexpected remote request: " + url)
        result = self.routes[url]
        if isinstance(result, Exception):
            raise result
        return result

    def json(self, url, obj):
        self.routes[url] = encoded(obj), {}

    def github(self, contents, revision="a" * 40):
        api = "https://api.github.com/repos/fluxerapp/fluxer"
        tree_ids = {"root": "1" * 40, "deploy": "2" * 40, "helm": "3" * 40,
                    **{name: str(i + 4) * 40 for i, name in enumerate(update.CHARTS)}}

        def directory(name, key):
            return {"path": name, "type": "tree", "mode": "040000", "sha": tree_ids[key]}

        def blob(name, data):
            return {"path": name, "type": "blob", "mode": "100644", "sha": update.git_blob_sha(data), "size": len(data)}

        self.json(api + "/commits/main", {"sha": revision, "commit": {"tree": {"sha": tree_ids["root"]}}})
        self.json(api + "/commits/" + revision, {"sha": revision, "commit": {"tree": {"sha": tree_ids["root"]}}})
        trees = {"root": [directory("deploy", "deploy"), blob("LICENSE", contents["LICENSE"])],
                 "deploy": [directory("helm", "helm")],
                 "helm": [directory(name, name) for name in update.CHARTS]}
        for chart in update.CHARTS:
            trees[chart] = [blob(name.removeprefix(chart + "/"), data) for name, data in sorted(contents.items())
                            if name.startswith(chart + "/")]
        for key, entries in trees.items():
            self.json(api + "/git/trees/" + tree_ids[key] + ("?recursive=1" if key in update.CHARTS else ""),
                      {"sha": tree_ids[key], "truncated": False, "tree": entries})
        for name, data in contents.items():
            remote = "LICENSE" if name == "LICENSE" else "deploy/helm/" + name
            self.routes["https://raw.githubusercontent.com/fluxerapp/fluxer/" + revision + "/" + remote] = data, {}
        return tree_ids

    def registry(self, repository, platform="amd64", index=True):
        name = repository.removeprefix("ghcr.io/")
        query = urllib.parse.urlencode({"service": "ghcr.io", "scope": "repository:" + name + ":pull"})
        self.json("https://ghcr.io/token?" + query, {"token": "do-not-log-this-token"})
        prefix = "https://ghcr.io/v2/" + name
        config = encoded({"architecture": platform, "os": "linux", "config": {
            "Entrypoint": ["/entrypoint.sh"], "Cmd": ["node", "dist/server.js"],
            "WorkingDir": "/app", "User": "65532", "Env": ["BUILD_VERSION=v1", "PRIVATE_TEST=not-for-the-report"],
            "Labels": {"org.opencontainers.image.revision": hashlib.sha1(repository.encode()).hexdigest()}}})
        config_desc = descriptor(config, "application/vnd.oci.image.config.v1+json")
        manifest = encoded({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "config": config_desc, "layers": []})
        digest = update.sha256(manifest)
        child = descriptor(manifest, "application/vnd.oci.image.manifest.v1+json")
        child["platform"] = {"os": "linux", "architecture": "amd64"}
        index_body = encoded({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                              "manifests": [child, {"platform": {"os": "linux", "architecture": "arm64"}}]})
        tag_body = index_body if index else manifest
        self.routes[prefix + "/manifests/v1"] = tag_body, {"docker-content-digest": update.sha256(tag_body)}
        self.routes[prefix + "/manifests/" + digest] = manifest, {"docker-content-digest": digest}
        self.routes[prefix + "/blobs/" + config_desc["digest"]] = config, {}
        return digest, prefix, config_desc


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        (self.root / update.APP).mkdir(parents=True)
        shutil.copytree(REPO / update.CHART_ROOT, self.root / update.CHART_ROOT)
        (self.root / update.CONTRACT).parent.mkdir(parents=True)
        shutil.copy2(REPO / update.CONTRACT, self.root / update.CONTRACT)
        shutil.copy2(REPO / update.LOCK, self.root / update.LOCK)
        self.lock = json.loads((self.root / update.LOCK).read_text())
        for release in self.lock["releases"]:
            filename = "helmrelease-" + release.removeprefix("fluxer-") + ".yaml"
            shutil.copy2(REPO / update.APP / filename, self.root / update.APP / filename)
        self.remote = Remote()

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def chart_contents(self):
        metadata = json.loads((self.root / update.CHART_ROOT / "UPSTREAM.json").read_text())
        return {name: (self.root / update.CHART_ROOT / name).read_bytes() for name in metadata["files"]}

    def registry_all(self):
        repositories = {r["repository"] for ws in self.lock["releases"].values() for cs in ws.values() for r in cs.values()}
        return {repo: self.remote.registry(repo)[0] for repo in sorted(repositories)}

    def mutate_route_json(self, url, transform, rehash=False):
        raw, headers = self.remote.routes[url]
        obj = json.loads(raw)
        transform(obj)
        raw = encoded(obj)
        self.remote.routes[url] = raw, {"docker-content-digest": update.sha256(raw)} if rehash else headers

    def test_same_chart_bytes_new_commit_is_noop(self):
        self.remote.github(self.chart_contents())
        before = self.snapshot()
        changes, report = update.chart_plan(self.root, self.remote)
        self.assertEqual({}, changes)
        self.assertIn("No chart content changed", "\n".join(report))
        self.assertEqual(before, self.snapshot())
        self.assertTrue(all("archive" not in url for url in self.remote.calls))

    def test_chart_update_deletion_version_labels_and_second_run_noop(self):
        obsolete = "fluxer-web/obsolete.txt"
        path = self.root / update.CHART_ROOT / obsolete
        path.write_bytes(b"old")
        provenance_path = self.root / update.CHART_ROOT / "UPSTREAM.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["files"][obsolete] = hashlib.sha256(b"old").hexdigest()
        provenance["review_policy"] = "keep-me"
        provenance_path.write_bytes(encoded(provenance))
        readme = self.root / update.CHART_ROOT / "README.md"
        readme.write_text("local docs")
        contents = self.chart_contents()
        del contents[obsolete]
        contents["fluxer-web/Chart.yaml"] = contents["fluxer-web/Chart.yaml"].replace(b"version: 0.1.0", b"version: 0.2.0")
        contents["fluxer-web/new.txt"] = b"new upstream content\n"
        self.remote.github(contents)
        before = self.snapshot()
        changes, _ = update.chart_plan(self.root, self.remote)
        self.assertIsNone(changes[update.CHART_ROOT / obsolete])
        update.apply_changes(self.root, changes)
        self.assertFalse(path.exists())
        self.assertEqual(readme.read_text(), "local docs")
        self.assertEqual(json.loads(provenance_path.read_text())["review_policy"], "keep-me")
        self.assertEqual(before[update.CONTRACT], (self.root / update.CONTRACT).read_bytes())
        for release in ["admin", "app-proxy"]:
            text = (self.root / update.APP / ("helmrelease-" + release + ".yaml")).read_text()
            self.assertIn("version: 0.2.0", text)
            self.assertIn("value: fluxer-web-0.2.0", text)
            self.assertIn("# Keep the running application version", text)
        self.assertEqual({}, update.chart_plan(self.root, self.remote)[0])

    def test_failed_last_chart_download_does_not_write(self):
        contents = self.chart_contents()
        contents["fluxer-web/values.yaml"] += b"\n# changed\n"
        self.remote.github(contents)
        url = next(url for url in self.remote.routes if url.endswith("/fluxer-web/values.yaml"))
        self.remote.routes[url] = b"tampered", {}
        before = self.snapshot()
        with self.assertRaisesRegex(update.UpdateError, "Git blob verification"):
            update.chart_plan(self.root, self.remote)
        self.assertEqual(before, self.snapshot())

    def test_remote_path_modes_missing_chart_license_and_truncation_rejected(self):
        for failure in ["../escape", "a/../../escape", "/absolute", "a\\escape", "symlink", "submodule", "missing-chart", "missing-license", "truncated"]:
            with self.subTest(failure=failure):
                self.remote = Remote()
                ids = self.remote.github(self.chart_contents())
                api = "https://api.github.com/repos/fluxerapp/fluxer/git/trees/"
                url = api + ids["fluxer-api"] + "?recursive=1"
                if failure in ["symlink", "submodule"]:
                    self.mutate_route_json(url, lambda obj: obj["tree"].append({"path": "bad", "type": "blob" if failure == "symlink" else "commit", "mode": "120000" if failure == "symlink" else "160000"}))
                elif failure == "missing-chart":
                    self.mutate_route_json(api + ids["helm"], lambda obj: obj["tree"].pop())
                elif failure == "missing-license":
                    self.mutate_route_json(api + ids["root"], lambda obj: obj["tree"].pop())
                elif failure == "truncated":
                    self.mutate_route_json(url, lambda obj: obj.update(truncated=True))
                else:
                    self.mutate_route_json(url, lambda obj: obj["tree"].append({"path": failure}))
                before = self.snapshot()
                with self.assertRaises(update.UpdateError):
                    update.chart_plan(self.root, self.remote)
                self.assertEqual(before, self.snapshot())

    def test_missing_required_file_and_remote_dependency_rejected(self):
        for failure in ["missing-values", "dependency"]:
            with self.subTest(failure=failure):
                contents = self.chart_contents()
                if failure == "missing-values":
                    del contents["fluxer-api/values.yaml"]
                else:
                    contents["fluxer-api/Chart.yaml"] += b"\ndependencies:\n  - name: external\n    repository: https://example.com/charts\n    version: 1.0.0\n"
                self.remote = Remote()
                self.remote.github(contents)
                with self.assertRaises(update.UpdateError):
                    update.chart_plan(self.root, self.remote)

    def test_verify_upstream_rejects_tampering_even_with_recomputed_local_hashes(self):
        contents = self.chart_contents()
        pinned = json.loads((self.root / update.CHART_ROOT / "UPSTREAM.json").read_text())["commit"]
        self.remote.github(contents, pinned)
        self.assertEqual({}, update.chart_plan(self.root, self.remote, verify=True)[0])
        name = "fluxer-api/values.yaml"
        path = self.root / update.CHART_ROOT / name
        path.write_bytes(path.read_bytes() + b"\n# unauthorized change\n")
        metadata_path = self.root / update.CHART_ROOT / "UPSTREAM.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata_path.write_bytes(encoded(metadata))
        with self.assertRaisesRegex(update.UpdateError, "exactly match"):
            update.chart_plan(self.root, self.remote, verify=True)

    def test_unlisted_local_file_and_symlink_rejected(self):
        path = self.root / update.CHART_ROOT / "fluxer-api/unlisted.yaml"
        path.write_text("{}")
        with self.assertRaisesRegex(update.UpdateError, "inventory"):
            update.chart_plan(self.root, self.remote)
        path.unlink()
        path.symlink_to("values.yaml")
        with self.assertRaisesRegex(update.UpdateError, "Symlink"):
            update.chart_plan(self.root, self.remote)

    def test_registry_returns_child_digest_not_index_and_bounded_metadata(self):
        repo = "ghcr.io/fluxerapp/fluxer-api"
        digest, _, _ = self.remote.registry(repo)
        actual = update.Registry(self.remote).image(repo)
        self.assertEqual(digest, actual["digest"])
        self.assertEqual("v1", actual["runtime"]["build_version"])
        self.assertNotIn("PRIVATE_TEST", json.dumps(actual))

    def test_direct_platform_manifest_supported(self):
        repo = "ghcr.io/fluxerapp/fluxer-api"
        digest, _, _ = self.remote.registry(repo, index=False)
        self.assertEqual(digest, update.Registry(self.remote).image(repo)["digest"])
        self.assertEqual(digest, update.Registry(self.remote).image(repo, digest)["digest"])

    def test_registry_index_manifest_and_config_digest_mismatches_rejected(self):
        for stage in ["index", "manifest", "config"]:
            with self.subTest(stage=stage):
                self.remote = Remote()
                repo = "ghcr.io/fluxerapp/fluxer-api"
                digest, prefix, config = self.remote.registry(repo)
                url = prefix + ({"index": "/manifests/v1", "manifest": "/manifests/" + digest,
                                 "config": "/blobs/" + config["digest"]}[stage])
                raw, headers = self.remote.routes[url]
                self.remote.routes[url] = raw + b" ", headers
                with self.assertRaisesRegex(update.UpdateError, "digest mismatch"):
                    update.Registry(self.remote).image(repo)

    def test_registry_wrong_platform_missing_platform_and_ambiguous_index_rejected(self):
        repo = "ghcr.io/fluxerapp/fluxer-api"
        self.remote.registry(repo, platform="arm64")
        with self.assertRaisesRegex(update.UpdateError, "config platform"):
            update.Registry(self.remote).image(repo)
        for mode in ["missing", "ambiguous"]:
            self.remote = Remote()
            _, prefix, _ = self.remote.registry(repo)
            if mode == "missing":
                change = lambda obj: obj["manifests"][0].pop("platform")
            else:
                change = lambda obj: obj["manifests"].append(obj["manifests"][0])
            self.mutate_route_json(prefix + "/manifests/v1", change, rehash=True)
            with self.assertRaisesRegex(update.UpdateError, "exactly one"):
                update.Registry(self.remote).image(repo)

    def test_images_update_all_roles_preserve_text_then_noop(self):
        digests = self.registry_all()
        before = self.snapshot()
        changes, report = update.image_plan(self.root, self.remote)
        self.assertIn("source revisions are mixed", "\n".join(report))
        self.assertNotIn("do-not-log-this-token", "\n".join(report))
        self.assertNotIn("not-for-the-report", "\n".join(report))
        self.assertEqual(10, len(changes))  # Nine HelmReleases and one lock.
        for path, content in changes.items():
            if path != update.LOCK:
                strip = lambda data: re.sub(rb"sha256:[0-9a-f]{64}", b"DIGEST", data)
                self.assertEqual(strip(before[path]), strip(content))
        update.apply_changes(self.root, changes)
        updated = update.load_lock(self.root)
        for workloads in updated["releases"].values():
            for containers in workloads.values():
                for record in containers.values():
                    self.assertEqual(digests[record["repository"]], record["digest"])
        self.assertEqual(before[update.CONTRACT], (self.root / update.CONTRACT).read_bytes())
        self.assertEqual({}, update.image_plan(self.root, self.remote)[0])

    def test_failed_image_acquisition_does_not_write(self):
        self.registry_all()
        url = "https://ghcr.io/v2/fluxerapp/fluxer-users/manifests/v1"
        self.remote.routes[url] = update.UpdateError("mock failed last repository")
        before = self.snapshot()
        with self.assertRaises(update.UpdateError):
            update.image_plan(self.root, self.remote)
        self.assertEqual(before, self.snapshot())

    def test_verify_images_uses_immutable_refs_allows_legacy_metadata_and_checks_new_metadata(self):
        self.registry_all()
        changes, _ = update.image_plan(self.root, self.remote)
        update.apply_changes(self.root, changes)
        self.remote.calls.clear()
        self.assertEqual({}, update.image_plan(self.root, self.remote, verify=True)[0])
        self.assertFalse(any(url.endswith("/manifests/v1") for url in self.remote.calls))
        lock_path = self.root / update.LOCK
        lock = json.loads(lock_path.read_text())
        for workloads in lock["releases"].values():
            for containers in workloads.values():
                for record in containers.values():
                    for key in ["runtime", "source_revision", "config_descriptor"]:
                        record.pop(key, None)
        lock_path.write_bytes(encoded(lock))
        self.assertEqual({}, update.image_plan(self.root, self.remote, verify=True)[0])
        record = lock["releases"]["fluxer-api"]["Deployment/api"]["api"]
        record["source_revision"] = "0" * 40
        lock_path.write_bytes(encoded(lock))
        with self.assertRaisesRegex(update.UpdateError, "metadata differs"):
            update.image_plan(self.root, self.remote, verify=True)

    def test_fixed_index_pins_verify_through_their_platform_child(self):
        self.registry_all()
        changes, _ = update.image_plan(self.root, self.remote)
        update.apply_changes(self.root, changes)
        lock_path = self.root / update.LOCK
        lock = json.loads(lock_path.read_text())
        record = lock["releases"]["fluxer-admin"]["Deployment/admin"]["admin"]
        prefix = "https://ghcr.io/v2/fluxerapp/fluxer-admin/manifests/"
        raw, headers = self.remote.routes[prefix + "v1"]
        index_digest = update.sha256(raw)
        self.remote.routes[prefix + index_digest] = raw, headers
        hr = self.root / update.APP / "helmrelease-admin.yaml"
        hr.write_text(hr.read_text().replace(record["digest"], index_digest))
        record["digest"] = index_digest
        lock_path.write_bytes(encoded(lock))
        changes, report = update.image_plan(self.root, self.remote, verify=True)
        self.assertEqual({}, changes)
        self.assertIn("1 immutable OCI index pins", "\n".join(report))

    def test_check_only_cli_writes_report_without_repository_changes(self):
        self.registry_all()
        before = self.snapshot()
        report = Path(self.temp.name) / "report.md"
        with mock.patch.object(update, "HTTP", return_value=self.remote):
            result = update.main(["--kind", "images", "--repo-root", str(self.root), "--check-only", "--report", str(report)])
        self.assertEqual(0, result)
        self.assertEqual(before, self.snapshot())
        self.assertIn("no update was written", report.read_text())

    def test_read_only_report_cannot_overwrite_repository_file(self):
        before = self.snapshot()
        result = update.main(["--kind", "images", "--repo-root", str(self.root), "--check-only",
                              "--report", str(self.root / update.CONTRACT)])
        self.assertEqual(1, result)
        self.assertEqual(before, self.snapshot())

    def test_publish_failure_restores_already_written_files(self):
        first = update.APP / "helmrelease-admin.yaml"
        second = update.APP / "helmrelease-api.yaml"
        before = self.snapshot()
        real_replace = os.replace
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated filesystem failure")
            return real_replace(*args)

        with mock.patch.object(update.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(update.UpdateError, "original files restored"):
                update.apply_changes(self.root, {first: b"new1", second: b"new2"})
        self.assertEqual(before, self.snapshot())

    def test_publish_failure_atomically_restores_read_only_file_and_mode(self):
        first = update.APP / "helmrelease-admin.yaml"
        second = update.APP / "helmrelease-api.yaml"
        (self.root / first).chmod(0o444)
        before = self.snapshot()
        real_replace = os.replace
        replacements = []

        def fail_second(source, target):
            replacements.append((Path(source), Path(target)))
            if Path(target) == self.root / second:
                raise OSError("simulated apply failure after read-only file replacement")
            return real_replace(source, target)

        with mock.patch.object(update.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(update.UpdateError, "original files restored"):
                update.apply_changes(self.root, {first: b"new1", second: b"new2"})
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0o444, (self.root / first).stat().st_mode & 0o777)
        self.assertEqual(3, len(replacements))
        self.assertIn("originals", replacements[-1][0].parts)
        self.assertFalse(list(self.root.glob(".fluxer-update-*")))

    def test_rollback_failure_continues_restoring_and_retains_failed_original(self):
        first = update.APP / "helmrelease-admin.yaml"
        second = update.APP / "helmrelease-api.yaml"
        third = update.APP / "helmrelease-app-proxy.yaml"
        fourth = update.APP / "helmrelease-gateway.yaml"
        (self.root / third).chmod(0o444)
        before = self.snapshot()
        real_replace = os.replace

        def fail_apply_and_one_restore(source, target):
            source, target = Path(source), Path(target)
            if target == self.root / fourth and "updates" in source.parts:
                raise OSError("simulated apply failure")
            if target == self.root / third and "originals" in source.parts:
                raise PermissionError("simulated individual restore failure")
            return real_replace(source, target)

        with mock.patch.object(update.os, "replace", side_effect=fail_apply_and_one_restore):
            with self.assertRaises(update.UpdateError) as raised:
                update.apply_changes(self.root, {first: b"new1", second: b"new2", third: b"new3", fourth: b"new4"})
        self.assertIn("rollback failed for: " + str(third), str(raised.exception))
        self.assertIn("Recovery files retained at:", str(raised.exception))
        for restored in [first, second, fourth]:
            self.assertEqual(before[restored], (self.root / restored).read_bytes())
        self.assertEqual(b"new3", (self.root / third).read_bytes())
        recovery = list(self.root.glob(".fluxer-update-*"))
        self.assertEqual(1, len(recovery))
        original = recovery[0] / "originals" / third
        self.assertEqual(before[third], original.read_bytes())
        self.assertEqual(0o444, original.stat().st_mode & 0o777)
        self.assertIn(str(recovery[0] / "originals"), str(raised.exception))

    def test_redirect_strips_cross_host_authorization_and_rejects_http(self):
        request = urllib.request.Request("https://ghcr.io/blob", headers={"Authorization": "Bearer private"})
        redirect = update.SafeRedirect()
        result = redirect.redirect_request(request, None, 302, "Found", {}, "https://blob.example.org/signed")
        self.assertFalse(result.has_header("Authorization"))
        with self.assertRaises(update.UpdateError):
            redirect.redirect_request(request, None, 302, "Found", {}, "http://example.org/blob")


if __name__ == "__main__":
    unittest.main(verbosity=2)
