#!/usr/bin/env python3
"""Render pinned upstream charts and check compatibility with deployed releases.

Run with: uv run --with pyyaml==6.0.3 python tests/scripts/fluxer-chart-test.py
The historical contract fixes resource identities and runtime configuration.
The image lock permits digest updates without changing that historical contract.
No Kubernetes connection or decrypted secrets are needed.
"""

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
APP = REPO / "clusters/main/apps/fluxer"
CONTRACT = REPO / "tests/fixtures/fluxer-deployment-contract.json"
IMAGE_LOCK = APP / "images.lock.json"
CHARTS = {"fluxer-api", "fluxer-gateway", "fluxer-media-proxy", "fluxer-svc", "fluxer-web"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    """Normalize equivalent API defaults and migration-only chart labels."""
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: canonical(v) for k, v in value.items()
              if k not in {"helm.sh/chart", "app.kubernetes.io/component", "app.kubernetes.io/part-of"}}
    if "env" in result:
        result["env"] = sorted(result["env"], key=lambda e: e["name"])
    if result.get("protocol") == "TCP":
        result.pop("protocol")
    if result.get("type") == "ClusterIP":
        result.pop("type")
    if result.get("targetPort") == result.get("port") and "targetPort" in result:
        result.pop("targetPort")
    if result.get("imagePullPolicy") == "IfNotPresent":
        result.pop("imagePullPolicy")
    if result.get("apiVersion") == "v1" and "fieldPath" in result:
        result.pop("apiVersion")
    if "image" in result:
        result["image"] = result["image"].split("@", 1)[0]
    if result.get("imagePullSecrets") == [{"name": "ghcr-pull-secret"}]:
        # This legacy reference does not exist in the cluster and is removed.
        result.pop("imagePullSecrets")
    return result


def spec_digest(spec):
    serialized = json.dumps(canonical(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def check_chart_dependencies(chart):
    dependencies = yaml.safe_load((chart / "Chart.yaml").read_text()).get("dependencies", [])
    for dependency in dependencies:
        repository = dependency.get("repository", "")
        require(not repository or repository.startswith("file://"),
                str(chart) + ": remote chart dependency is forbidden: " + repository)
    require(not dependencies, str(chart) + ": vendored charts must not require dependency resolution")


def check_provenance(chart_root):
    """Check every vendored file, including files absent from the manifest."""
    require(chart_root.is_dir() and not chart_root.is_symlink(), "Unsafe vendored chart root")
    files = set()
    directories = set()
    pending = [chart_root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            mode = path.lstat().st_mode
            require(not stat.S_ISLNK(mode), "Vendored symlink is forbidden: " + str(path))
            if stat.S_ISDIR(mode):
                if directory == chart_root:
                    directories.add(path.name)
                pending.append(path)
            else:
                require(stat.S_ISREG(mode), "Nonregular vendored file: " + str(path))
                files.add(path.relative_to(chart_root).as_posix())
    require(directories == CHARTS, "Vendored chart directory inventory changed")
    provenance = json.loads((chart_root / "UPSTREAM.json").read_text())
    require(re.fullmatch(r"[0-9a-f]{40}", provenance["commit"]), "Missing upstream commit")
    manifest = provenance["files"]
    require(isinstance(manifest, dict), "Invalid upstream file manifest")
    for filename, digest in manifest.items():
        relative = pathlib.PurePosixPath(filename)
        require(not relative.is_absolute() and relative.as_posix() == filename and
                not any(part in {".", ".."} for part in relative.parts), "Unsafe provenance path")
        require(re.fullmatch(r"[0-9a-f]{64}", digest), "Invalid provenance digest: " + filename)
    actual = files - {"README.md", "UPSTREAM.json"}
    require(actual == set(manifest),
            "Vendored file inventory changed; unregistered=" + str(sorted(actual - set(manifest))) +
            "; missing=" + str(sorted(set(manifest) - actual)))
    require("LICENSE" in actual, "Vendored upstream LICENSE missing")
    for filename, digest in manifest.items():
        require(hashlib.sha256((chart_root / filename).read_bytes()).hexdigest() == digest,
                "Vendored upstream content changed: " + filename)
    for name in sorted(CHARTS):
        check_chart_dependencies(chart_root / name)


def check_image_lock(lock, contracts):
    """Digests may change; release, container, repository, and channel may not."""
    require(lock.get("schema_version") == 1, "Unsupported image lock schema")
    require(lock.get("channel") == "v1", "Image lock channel changed")
    require(lock.get("platform") == "linux/amd64", "Image lock platform changed")
    releases = lock.get("releases", {})
    require(set(releases) == set(contracts), "Image lock release inventory changed")
    for name, contract in contracts.items():
        require(set(releases[name]) == set(contract["workloads"]),
                name + ": image lock workload inventory changed")
        for key, expected in contract["workloads"].items():
            containers = releases[name][key]
            require(set(containers) == set(expected["images"]),
                    key + ": image lock container inventory changed")
            for container_name, record in containers.items():
                require(record.get("repository") == expected["repositories"][container_name],
                        key + ": image lock repository changed")
                require(record.get("tag") == "v1", key + ": image lock tag changed")
                require(isinstance(record.get("digest"), str) and
                        re.fullmatch(r"sha256:[0-9a-f]{64}", record["digest"]),
                        key + ": invalid image lock digest")
    return releases


def build(directory):
    command = (["kustomize", "build"] if shutil.which("kustomize")
               else ["kubectl", "kustomize"])
    return subprocess.check_output(command + [str(directory)], text=True)


def render(hr, output, chart_version=None):
    name = hr["metadata"]["name"]
    spec = hr["spec"]
    chart_spec = spec["chart"]["spec"]
    require(spec.get("upgrade", {}).get("chartNameChangeStrategy") == "InPlaceUpdate",
            name + ": chart name changes must not uninstall the release")
    require(chart_spec.get("reconcileStrategy") == "Revision",
            name + ": chart changes must track source revisions")
    source = chart_spec["sourceRef"]
    require(source["kind"] == "GitRepository" and source["name"] == "flux-system" and
            source["namespace"] == "flux-system", name + ": use the existing infra source")
    chart = (REPO / chart_spec["chart"]).resolve()
    require(chart.is_relative_to((REPO / "charts/fluxer/current").resolve()),
            name + ": chart escapes the vendored source")
    require((chart / "Chart.yaml").is_file(), name + ": referenced chart does not exist")
    directory = output / name
    directory.mkdir(parents=True, exist_ok=True)
    if chart_version:
        modified = directory / "chart"
        shutil.copytree(chart, modified)
        metadata = yaml.safe_load((modified / "Chart.yaml").read_text())
        metadata["version"] = chart_version
        (modified / "Chart.yaml").write_text(yaml.safe_dump(metadata))
        chart = modified
    values = directory / "values.yaml"
    values.write_text(yaml.safe_dump(spec.get("values", {}), allow_unicode=True))
    check_chart_dependencies(chart)
    raw = subprocess.check_output([
        "helm", "template", name, str(chart), "-n", "fluxer", "--is-upgrade", "-f", str(values)
    ], text=True)
    (directory / "raw.yaml").write_text(raw)
    for index, renderer in enumerate(spec.get("postRenderers", [])):
        # Renderers run in order, matching HelmRelease semantics.
        overlay = directory / ("post-render-" + str(index))
        overlay.mkdir()
        (overlay / "resources.yaml").write_text(raw)
        kustomization = {"apiVersion": "kustomize.config.k8s.io/v1beta1",
                         "kind": "Kustomization", "resources": ["resources.yaml"]}
        kustomization.update(renderer["kustomize"])
        (overlay / "kustomization.yaml").write_text(yaml.safe_dump(kustomization))
        raw = build(overlay)
    (directory / "final.yaml").write_text(raw)
    return [item for item in yaml.safe_load_all(raw) if item]


def check_release(name, objects, contract, declared_secrets, image_lock):
    indexed = {o["kind"] + "/" + o["metadata"]["name"]: o for o in objects}
    require(len(indexed) == len(objects), name + ": duplicate resource identity")
    require(sorted(indexed) == contract["resources"], name + ": resource inventory changed")
    for key, item in indexed.items():
        metadata = item["metadata"]
        require(metadata.get("namespace", "fluxer") == contract["namespaces"][key],
                key + ": namespace changed")
        # Top-level metadata is outside the runtime spec digest but controls
        # Helm ownership, lifecycle hooks, and whether resources can be deleted.
        labels = metadata.get("labels", {})
        require(isinstance(labels, dict) and
                labels.get("app.kubernetes.io/managed-by") == "Helm" and
                labels.get("app.kubernetes.io/instance") == name,
                key + ": Helm ownership labels changed")
        annotations = metadata.get("annotations") or {}
        require(isinstance(annotations, dict), key + ": invalid resource annotations")
        require(not any(annotation.startswith("helm.sh/hook") or
                        annotation == "helm.sh/resource-policy" for annotation in annotations),
                key + ": Helm lifecycle annotations are forbidden")
        for annotation, expected in {"meta.helm.sh/release-name": name,
                                     "meta.helm.sh/release-namespace": contract["namespaces"][key]}.items():
            require(annotation not in annotations or annotations[annotation] == expected,
                    key + ": Helm ownership annotation changed: " + annotation)
        require(not metadata.get("ownerReferences"), key + ": ownerReferences are forbidden")
        require(not metadata.get("finalizers"), key + ": finalizers are forbidden")
        require(spec_digest(item["spec"]) == contract["spec_digests"][key],
                key + ": deployed runtime or network configuration changed")
    for key, selector in contract["selectors"].items():
        require(indexed[key]["spec"]["selector"] == selector, key + ": selector changed")
    for key, expected in contract["workloads"].items():
        spec = indexed[key]["spec"]
        require(spec["selector"] == expected["selector"], key + ": immutable selector changed")
        labels = spec["template"]["metadata"]["labels"]
        require(all(labels.get(k) == v for k, v in spec["selector"]["matchLabels"].items()),
                key + ": pod labels do not match the selector")
        for field, value in expected.get("immutable", {}).items():
            require(spec.get(field) == value, key + ": immutable " + field + " changed")
        pod = spec["template"]["spec"]
        containers = {c["name"]: c for c in pod["containers"]}
        require(set(containers) == set(expected["images"]), key + ": containers changed")
        for container_name in expected["images"]:
            container = containers[container_name]
            record = image_lock[key][container_name]
            locked_image = record["repository"] + ":" + record["tag"] + "@" + record["digest"]
            require(container["image"] == locked_image,
                    key + ": rendered image differs from image lock: " + container_name)
            env = container.get("env", [])
            env_names = [e["name"] for e in env]
            require(len(env_names) == len(set(env_names)), key + ": duplicate environment variable")
            seen = set()
            for entry in env:
                dependencies = set(re.findall(r"\$\(([A-Za-z_][A-Za-z_0-9]*)\)", entry.get("value", "")))
                require(dependencies <= seen, key + ": environment expansion precedes its dependency")
                seen.add(entry["name"])
            require(not any("example.com" in str(e.get("value", "")) for e in env),
                    key + ": example endpoint in environment")
            references = [e.get("valueFrom", {}).get("secretKeyRef", {}).get("name") for e in env]
            references += [e.get("secretRef", {}).get("name") for e in container.get("envFrom", [])]
            require(all(not ref or ref in declared_secrets for ref in references),
                    key + ": undeclared Secret reference")
            if key == "Deployment/api":
                command = "\n".join(container.get("command", []))
                require("Config.nats.coreUrl = natsUrl" in command and
                        "tsx /tmp/fluxer-api-wrapper.ts" in command,
                        "API NATS startup wrapper missing")
        references = [s["name"] for s in pod.get("imagePullSecrets", [])]
        references += [v.get("secret", {}).get("secretName") for v in pod.get("volumes", [])
                       if not v.get("secret", {}).get("optional", False)]
        require(all(not ref or ref in declared_secrets for ref in references),
                key + ": undeclared pod Secret reference")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=pathlib.Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fluxer-chart-test-") as temporary:
        temp = pathlib.Path(temporary)
        output = args.output_dir or temp / "output"
        output.mkdir(parents=True, exist_ok=True)
        chart_root = REPO / "charts/fluxer/current"
        check_provenance(chart_root)
        objects = [o for o in yaml.safe_load_all(build(APP)) if o]
        secrets = {o["metadata"]["name"] for o in objects if o["kind"] == "Secret"}
        # CNPG creates these resources from the checked-in Cluster declaration.
        secrets.update({"fluxer-db-app", "fluxer-db-ca"})
        contracts = json.loads(CONTRACT.read_text())
        image_locks = check_image_lock(json.loads(IMAGE_LOCK.read_text()), contracts)
        releases = {o["metadata"]["name"]: o for o in objects
                    if o["kind"] == "HelmRelease" and o["metadata"]["name"] in contracts}
        require(set(releases) == set(contracts), "Missing migrated HelmRelease")
        combined = []
        owners = set()
        for name, hr in sorted(releases.items()):
            rendered = render(hr, output)
            check_release(name, rendered, contracts[name], secrets, image_locks[name])
            # Flux Revision artifacts append a source SHA to Chart.Version.
            # Unrelated infra commits must not alter any Pod template.
            version = yaml.safe_load((REPO / hr["spec"]["chart"]["spec"]["chart"] / "Chart.yaml").read_text())["version"]
            revised = render(hr, output / "revision", version + "+aaaaaaaaaaaa")
            original_pods = {o["kind"] + "/" + o["metadata"]["name"]: o["spec"]["template"]
                             for o in rendered if o["kind"] in {"Deployment", "StatefulSet"}}
            revised_pods = {o["kind"] + "/" + o["metadata"]["name"]: o["spec"]["template"]
                            for o in revised if o["kind"] in {"Deployment", "StatefulSet"}}
            require(original_pods == revised_pods, name + ": source revision changes the Pod template")
            for item in rendered:
                identity = (item["kind"], item["metadata"].get("namespace", "fluxer"),
                            item["metadata"]["name"])
                require(identity not in owners, "Resource owned by multiple releases: " + str(identity))
                owners.add(identity)
            combined.extend(rendered)
            print(name + ": render and deployment contract passed")
        rendered_file = output / "all.yaml"
        rendered_file.write_text(yaml.safe_dump_all(combined))
        if shutil.which("kubeconform"):
            subprocess.run(["kubeconform", "-strict", "-summary", str(rendered_file)], check=True)
        print("Validated " + str(len(releases)) + " releases / " + str(len(combined)) + " resources")


if __name__ == "__main__":
    main()
