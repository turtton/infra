#!/usr/bin/env python3
"""Offline positive/negative tests for the Fluxer chart update contract.

Run: uv run --with pyyaml==6.0.3 python tests/scripts/test-fluxer-chart-contract.py
"""

import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import unittest

import yaml

MODULE_PATH = pathlib.Path(__file__).with_name("fluxer-chart-test.py")
SPEC = importlib.util.spec_from_file_location("fluxer_chart_contract", MODULE_PATH)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)
NEW_DIGEST = "sha256:" + "a" * 64


class DeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="fluxer-contract-unit-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.output = pathlib.Path(cls.temporary.name)
        cls.contracts = json.loads(validator.CONTRACT.read_text())
        objects = [o for o in yaml.safe_load_all(validator.build(validator.APP)) if o]
        cls.secrets = {o["metadata"]["name"] for o in objects if o["kind"] == "Secret"}
        cls.secrets.update({"fluxer-db-app", "fluxer-db-ca"})
        cls.releases = {o["metadata"]["name"]: o for o in objects
                        if o["kind"] == "HelmRelease" and o["metadata"]["name"] in cls.contracts}
        cls.rendered = {name: validator.render(hr, cls.output / "baseline")
                        for name, hr in cls.releases.items()}
        # Generate an isolated lock from renders so tests also work before the
        # initial checked-in lock exists. Historical repositories stay authoritative.
        cls.lock = {"schema_version": 1, "channel": "v1", "platform": "linux/amd64", "releases": {}}
        for name, contract in cls.contracts.items():
            indexed = {o["kind"] + "/" + o["metadata"]["name"]: o for o in cls.rendered[name]}
            cls.lock["releases"][name] = {}
            for key, workload in contract["workloads"].items():
                containers = indexed[key]["spec"]["template"]["spec"]["containers"]
                cls.lock["releases"][name][key] = {
                    c["name"]: {"repository": workload["repositories"][c["name"]], "tag": "v1",
                                "digest": c["image"].split("@", 1)[1]}
                    for c in containers}

    def setUp(self):
        self.lock = copy.deepcopy(self.__class__.lock)
        self.objects = copy.deepcopy(self.rendered["fluxer-api"])
        self.contract = copy.deepcopy(self.contracts["fluxer-api"])
        self.api = next(o for o in self.objects if o["kind"] == "Deployment")
        self.container = self.api["spec"]["template"]["spec"]["containers"][0]

    def check_api(self):
        locks = validator.check_image_lock(self.lock, self.contracts)
        validator.check_release("fluxer-api", self.objects, self.contract, self.secrets, locks["fluxer-api"])

    def accept_test_runtime(self):
        # Isolate secondary guards from the first runtime-hash guard. This
        # modifies a test-only in-memory fixture, never the historical JSON file.
        self.contract["spec_digests"]["Deployment/api"] = validator.spec_digest(self.api["spec"])

    def test_all_releases_preserve_historical_contract(self):
        locks = validator.check_image_lock(self.lock, self.contracts)
        owners = set()
        for name, objects in self.rendered.items():
            validator.check_release(name, objects, self.contracts[name], self.secrets, locks[name])
            for obj in objects:
                owner = obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"]
                self.assertNotIn(owner, owners)
                owners.add(owner)
        self.assertEqual(len(owners), 48)

    def updated_api_render(self, directory):
        hr = copy.deepcopy(self.releases["fluxer-api"])
        hr["spec"]["values"]["api"]["api"]["image"]["digest"] = NEW_DIGEST
        return validator.render(hr, self.output / directory)

    def test_matching_lock_and_helmrelease_digest_update_is_allowed(self):
        self.objects = self.updated_api_render("matching-update")
        self.lock["releases"]["fluxer-api"]["Deployment/api"]["api"]["digest"] = NEW_DIGEST
        self.check_api()

    def test_helmrelease_digest_update_without_lock_is_rejected(self):
        self.objects = self.updated_api_render("unlocked-update")
        with self.assertRaisesRegex(ValueError, "rendered image differs from image lock"):
            self.check_api()

    def test_lock_digest_update_without_helmrelease_is_rejected(self):
        self.lock["releases"]["fluxer-api"]["Deployment/api"]["api"]["digest"] = NEW_DIGEST
        with self.assertRaisesRegex(ValueError, "rendered image differs from image lock"):
            self.check_api()

    def test_lock_repository_change_is_rejected(self):
        self.lock["releases"]["fluxer-api"]["Deployment/api"]["api"]["repository"] = "ghcr.io/other/api"
        with self.assertRaisesRegex(ValueError, "image lock repository changed"):
            self.check_api()

    def test_lock_channel_platform_schema_and_tag_are_fixed(self):
        for field, value in [("channel", "latest"), ("platform", "linux/arm64"), ("schema_version", 2)]:
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.lock)
                candidate[field] = value
                with self.assertRaises(ValueError):
                    validator.check_image_lock(candidate, self.contracts)
        self.lock["releases"]["fluxer-api"]["Deployment/api"]["api"]["tag"] = "latest"
        with self.assertRaisesRegex(ValueError, "image lock tag changed"):
            self.check_api()

    def test_lock_requires_complete_exact_inventory(self):
        for level in ["release", "workload", "container"]:
            for operation in ["remove", "add"]:
                with self.subTest(level=level, operation=operation):
                    candidate = copy.deepcopy(self.lock)
                    collection = candidate["releases"]
                    if level in {"workload", "container"}:
                        collection = collection["fluxer-api"]
                    if level == "container":
                        collection = collection["Deployment/api"]
                    if operation == "remove":
                        collection.pop(next(iter(collection)))
                    else:
                        collection["unexpected"] = {}
                    with self.assertRaisesRegex(ValueError, "inventory changed"):
                        validator.check_image_lock(candidate, self.contracts)

    def test_invalid_lock_digest_is_rejected(self):
        for digest in ["sha256:abc", "sha256:" + "A" * 64, "sha512:" + "a" * 64, None]:
            with self.subTest(digest=digest):
                self.lock["releases"]["fluxer-api"]["Deployment/api"]["api"]["digest"] = digest
                with self.assertRaisesRegex(ValueError, "invalid image lock digest"):
                    self.check_api()

    def test_selector_change_is_rejected(self):
        self.api["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] = "other"
        with self.assertRaisesRegex(ValueError, "runtime or network configuration changed"):
            self.check_api()

    def test_namespace_and_resource_inventory_changes_are_rejected(self):
        self.api["metadata"]["namespace"] = "other"
        with self.assertRaisesRegex(ValueError, "namespace changed"):
            self.check_api()
        self.api["metadata"]["namespace"] = "fluxer"
        self.objects.append(copy.deepcopy(self.api))
        with self.assertRaisesRegex(ValueError, "duplicate resource identity"):
            self.check_api()
        self.objects.pop()
        self.objects.pop()
        with self.assertRaisesRegex(ValueError, "resource inventory changed"):
            self.check_api()

    def test_ownership_labels_are_required_on_every_resource(self):
        locks = validator.check_image_lock(self.lock, self.contracts)
        for name, baseline in self.rendered.items():
            for index, obj in enumerate(baseline):
                for label in ["app.kubernetes.io/managed-by", "app.kubernetes.io/instance"]:
                    for value in [None, "other"]:
                        with self.subTest(release=name, resource=obj["metadata"]["name"],
                                          label=label, value=value):
                            objects = copy.deepcopy(baseline)
                            labels = objects[index]["metadata"]["labels"]
                            if value is None:
                                labels.pop(label)
                            else:
                                labels[label] = value
                            with self.assertRaisesRegex(ValueError, "Helm ownership labels changed"):
                                validator.check_release(name, objects, self.contracts[name],
                                                        self.secrets, locks[name])

    def test_helm_hook_annotations_are_rejected(self):
        for annotation in ["helm.sh/hook", "helm.sh/hook-delete-policy", "helm.sh/hook-weight"]:
            with self.subTest(annotation=annotation):
                self.api["metadata"]["annotations"] = {annotation: "pre-upgrade"}
                with self.assertRaisesRegex(ValueError, "Helm lifecycle annotations are forbidden"):
                    self.check_api()

    def test_helm_resource_policy_annotation_is_rejected(self):
        for value in ["keep", ""]:
            with self.subTest(value=value):
                self.api["metadata"]["annotations"] = {"helm.sh/resource-policy": value}
                with self.assertRaisesRegex(ValueError, "Helm lifecycle annotations are forbidden"):
                    self.check_api()

    def test_other_helm_owner_annotations_are_rejected(self):
        for annotation in ["meta.helm.sh/release-name", "meta.helm.sh/release-namespace"]:
            with self.subTest(annotation=annotation):
                self.api["metadata"]["annotations"] = {annotation: "other"}
                with self.assertRaisesRegex(ValueError, "Helm ownership annotation changed"):
                    self.check_api()

    def test_owner_references_are_rejected(self):
        self.api["metadata"]["ownerReferences"] = [{"apiVersion": "v1", "kind": "ConfigMap",
                                                  "name": "other", "uid": "other-owner",
                                                  "controller": True}]
        with self.assertRaisesRegex(ValueError, "ownerReferences are forbidden"):
            self.check_api()

    def test_finalizers_are_rejected(self):
        self.api["metadata"]["finalizers"] = ["example.com/prevent-deletion"]
        with self.assertRaisesRegex(ValueError, "finalizers are forbidden"):
            self.check_api()

    def test_matching_helm_ownership_and_empty_lifecycle_metadata_are_allowed(self):
        for obj in self.objects:
            obj["metadata"].update({"annotations": {"meta.helm.sh/release-name": "fluxer-api",
                                                  "meta.helm.sh/release-namespace": "fluxer"},
                                    "ownerReferences": [], "finalizers": []})
        self.check_api()

    def test_api_command_environment_and_runtime_metadata_are_preserved(self):
        for mutation in ["command", "env", "metadata"]:
            with self.subTest(mutation=mutation):
                objects = copy.deepcopy(self.objects)
                if mutation == "metadata":
                    self.api["spec"]["template"]["metadata"]["annotations"] = {"unexpected": "value"}
                else:
                    self.container.pop(mutation)
                with self.assertRaisesRegex(ValueError, "runtime or network configuration changed"):
                    self.check_api()
                self.objects = objects
                self.api = next(o for o in self.objects if o["kind"] == "Deployment")
                self.container = self.api["spec"]["template"]["spec"]["containers"][0]

    def test_api_wrapper_guard_remains(self):
        self.container.pop("command")
        self.accept_test_runtime()
        with self.assertRaisesRegex(ValueError, "API NATS startup wrapper missing"):
            self.check_api()

    def test_environment_dependency_and_duplicate_guards_remain(self):
        self.container["env"].append({"name": "UNRESOLVED", "value": "$(MISSING)"})
        self.accept_test_runtime()
        with self.assertRaisesRegex(ValueError, "environment expansion precedes its dependency"):
            self.check_api()
        self.container["env"].pop()
        self.container["env"].append(copy.deepcopy(self.container["env"][0]))
        self.accept_test_runtime()
        with self.assertRaisesRegex(ValueError, "duplicate environment variable"):
            self.check_api()

    def test_undeclared_secret_guard_remains(self):
        self.container["env"].append({"name": "BAD_SECRET", "valueFrom": {
            "secretKeyRef": {"name": "undeclared", "key": "password"}}})
        self.accept_test_runtime()
        with self.assertRaisesRegex(ValueError, "undeclared Secret reference"):
            self.check_api()

    def test_statefulset_immutable_field_guard_remains(self):
        name = "fluxer-gateway"
        contract = copy.deepcopy(self.contracts[name])
        objects = copy.deepcopy(self.rendered[name])
        key, expected = next((key, expected) for key, expected in contract["workloads"].items()
                             if expected.get("immutable"))
        obj = next(o for o in objects if o["kind"] + "/" + o["metadata"]["name"] == key)
        field = next(iter(expected["immutable"]))
        obj["spec"][field] = "unexpected"
        contract["spec_digests"][key] = validator.spec_digest(obj["spec"])
        with self.assertRaisesRegex(ValueError, "immutable " + field + " changed"):
            validator.check_release(name, objects, contract, self.secrets, self.lock["releases"][name])

    def test_source_revision_does_not_change_any_pod_template(self):
        for name, hr in self.releases.items():
            with self.subTest(release=name):
                chart = validator.REPO / hr["spec"]["chart"]["spec"]["chart"]
                version = yaml.safe_load((chart / "Chart.yaml").read_text())["version"]
                revised = validator.render(hr, self.output / "revision", version + "+aaaaaaaaaaaa")
                def pods(objects):
                    return {o["kind"] + "/" + o["metadata"]["name"]: o["spec"]["template"]
                            for o in objects if o["kind"] in {"Deployment", "StatefulSet"}}
                self.assertEqual(pods(self.rendered[name]), pods(revised))


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fluxer-provenance-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name) / "charts"
        shutil.copytree(validator.REPO / "charts/fluxer/current", self.root)

    def rewrite_manifest(self, filename, digest=None):
        path = self.root / "UPSTREAM.json"
        data = json.loads(path.read_text())
        data["files"][filename] = digest or hashlib.sha256((self.root / filename).read_bytes()).hexdigest()
        path.write_text(json.dumps(data))

    def test_current_vendor_passes(self):
        validator.check_provenance(self.root)

    def test_unregistered_template_is_rejected(self):
        (self.root / "fluxer-api/templates/injected.yaml").write_text("kind: Secret\n")
        with self.assertRaisesRegex(ValueError, "unregistered=.*injected.yaml"):
            validator.check_provenance(self.root)

    def test_extra_root_file_and_chart_directory_are_rejected(self):
        (self.root / "extra.txt").write_text("extra")
        with self.assertRaisesRegex(ValueError, "Vendored file inventory changed"):
            validator.check_provenance(self.root)
        (self.root / "extra-chart").mkdir()
        with self.assertRaisesRegex(ValueError, "chart directory inventory changed"):
            validator.check_provenance(self.root)

    def test_missing_and_modified_manifest_files_are_rejected(self):
        path = self.root / "fluxer-api/values.yaml"
        path.write_text(path.read_text() + "# changed\n")
        with self.assertRaisesRegex(ValueError, "upstream content changed"):
            validator.check_provenance(self.root)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing=.*values.yaml"):
            validator.check_provenance(self.root)

    def test_symlink_file_directory_and_excluded_readme_are_rejected(self):
        for filename in ["fluxer-api/values.yaml", "fluxer-api/templates", "README.md"]:
            with self.subTest(filename=filename):
                path = self.root / filename
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                path.symlink_to("/tmp")
                with self.assertRaisesRegex(ValueError, "symlink is forbidden"):
                    validator.check_provenance(self.root)
                path.unlink()
                source = validator.REPO / "charts/fluxer/current" / filename
                if source.is_dir():
                    shutil.copytree(source, path)
                else:
                    shutil.copyfile(source, path)

    def test_nonregular_file_is_rejected(self):
        os.mkfifo(self.root / "fifo")
        with self.assertRaisesRegex(ValueError, "Nonregular vendored file"):
            validator.check_provenance(self.root)

    def test_unsafe_manifest_paths_are_rejected(self):
        for filename in ["../outside", "/etc/passwd", "fluxer-api/../LICENSE", "./LICENSE", "fluxer-api//values.yaml"]:
            with self.subTest(filename=filename):
                provenance = self.root / "UPSTREAM.json"
                original = provenance.read_text()
                self.rewrite_manifest(filename, "a" * 64)
                with self.assertRaisesRegex(ValueError, "Unsafe provenance path"):
                    validator.check_provenance(self.root)
                provenance.write_text(original)

    def test_remote_chart_dependency_is_rejected_even_with_matching_hash(self):
        chart = self.root / "fluxer-api/Chart.yaml"
        data = yaml.safe_load(chart.read_text())
        data["dependencies"] = [{"name": "injected", "version": "1.0.0", "repository": "https://example.com/charts"}]
        chart.write_text(yaml.safe_dump(data))
        self.rewrite_manifest("fluxer-api/Chart.yaml")
        with self.assertRaisesRegex(ValueError, "remote chart dependency is forbidden"):
            validator.check_provenance(self.root)


if __name__ == "__main__":
    unittest.main()
