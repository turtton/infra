#!/usr/bin/env python3
"""Stage verified Fluxer chart or image updates without changing runtime contracts.

Run with uv run --with pyyaml==6.0.3 python scripts/update-fluxer.py --kind charts
or --kind images. --check-only performs the same downloads and checks, but only
writes the optional --report (place it outside the repository for a read-only run).
This fetcher never clones Fluxer's history, downloads image layers, or runs code
from upstream. The separate deployment-contract check remains the compatibility
gate; neither that check nor OCI metadata proves application startup or operation.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import yaml

REPOSITORY = "fluxerapp/fluxer"
CHARTS = ("fluxer-api", "fluxer-gateway", "fluxer-media-proxy", "fluxer-svc", "fluxer-web")
CHART_ROOT = Path("charts/fluxer/current")
APP = Path("clusters/main/apps/fluxer")
LOCK = APP / "images.lock.json"
CONTRACT = Path("tests/fixtures/fluxer-deployment-contract.json")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json",
                "application/vnd.docker.container.image.v1+json"}
ACCEPT = ", ".join(sorted(INDEX_TYPES | MANIFEST_TYPES))


class UpdateError(ValueError):
    """A failed acquisition or invariant; messages never contain auth headers."""


def require(condition, message):
    if not condition:
        raise UpdateError(message)


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def decode_json(data, context):
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise UpdateError(context + ": invalid JSON") from None
    require(isinstance(value, dict), context + ": expected JSON object")
    return value


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def git_blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        require(urllib.parse.urlsplit(newurl).scheme == "https", "Refusing non-HTTPS redirect")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


class HTTP:
    def __init__(self):
        cafile = os.environ.get("SSL_CERT_FILE")
        if not cafile and Path("/etc/ssl/certs/ca-certificates.crt").exists():
            cafile = "/etc/ssl/certs/ca-certificates.crt"
        context = ssl.create_default_context(cafile=cafile)
        self.opener = urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPSHandler(context=context))

    def get(self, url, headers=None, limit=MAX_FILE):
        require(urllib.parse.urlsplit(url).scheme == "https", "Only HTTPS acquisition is permitted")
        request = urllib.request.Request(url, headers={"User-Agent": "infra-fluxer-update", **(headers or {})})
        try:
            with self.opener.open(request, timeout=45) as response:
                body = response.read(limit + 1)
                require(len(body) <= limit, "Remote response exceeds the size limit")
                return body, {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            # Do not print request headers, response bodies or signed redirect URLs.
            raise UpdateError("Remote acquisition failed with HTTP " + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise UpdateError("Remote acquisition failed (network or TLS error)") from None


def safe_relative(name):
    require(isinstance(name, str) and name and "\\" not in name and "\x00" not in name,
            "Unsafe remote or local path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(part not in {"", ".", ".."} for part in name.split("/")),
            "Unsafe remote or local path")
    require(not any(ord(c) < 32 for c in name), "Control character in path")
    return path


def managed_chart_file(name):
    path = safe_relative(name)
    require(name == "LICENSE" or (len(path.parts) > 1 and path.parts[0] in CHARTS),
            "File outside the chart allowlist")
    return path


def local_path(root, relative):
    relative = safe_relative(str(relative))
    path = root
    for part in relative.parts:
        path = path / part
        require(not path.is_symlink(), "Symlink in a managed local path")
    require(not path.exists() or path.is_file(), "Managed target is not a regular file")
    return path


class GitHub:
    def __init__(self, http):
        self.http = http
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            self.headers["Authorization"] = "Bearer " + token

    def api(self, path):
        raw, _ = self.http.get("https://api.github.com/repos/" + REPOSITORY + path, self.headers)
        return decode_json(raw, "GitHub API")

    def tree(self, sha, recursive=False):
        require(isinstance(sha, str) and SHA1.fullmatch(sha), "Invalid Git tree SHA")
        tree = self.api("/git/trees/" + sha + ("?recursive=1" if recursive else ""))
        require(tree.get("sha") == sha and tree.get("truncated") is False, "Invalid or truncated Git tree")
        entries = tree.get("tree")
        require(isinstance(entries, list), "Missing Git tree entries")
        indexed = {}
        for item in entries:
            name = item.get("path")
            safe_relative(name)
            require(name not in indexed, "Duplicate Git tree path")
            indexed[name] = item
        return indexed

    @staticmethod
    def directory(entries, name):
        item = entries.get(name, {})
        require(item.get("type") == "tree" and item.get("mode") == "040000", "Missing directory: " + name)
        return item["sha"]

    @staticmethod
    def blob(item):
        require(item.get("type") == "blob" and item.get("mode") in {"100644", "100755"},
                "Symlink, submodule, or unsupported entry in chart source")
        require(isinstance(item.get("sha"), str) and SHA1.fullmatch(item["sha"]), "Invalid blob SHA")
        require(isinstance(item.get("size"), int) and 0 <= item["size"] <= MAX_FILE, "Invalid blob size")

    def charts(self, ref):
        commit = self.api("/commits/" + urllib.parse.quote(ref, safe=""))
        sha = commit.get("sha", "")
        require(isinstance(sha, str) and SHA1.fullmatch(sha), "Invalid upstream commit SHA")
        if SHA1.fullmatch(ref):
            require(ref == sha, "Resolved commit differs from the requested immutable SHA")
        root = self.tree(commit.get("commit", {}).get("tree", {}).get("sha"))
        license_entry = root.get("LICENSE", {})
        self.blob(license_entry)
        deploy = self.tree(self.directory(root, "deploy"))
        helm = self.tree(self.directory(deploy, "helm"))
        files = {"LICENSE": ("LICENSE", license_entry)}
        for chart in CHARTS:
            entries = self.tree(self.directory(helm, chart), recursive=True)
            for name, item in entries.items():
                if item.get("type") == "tree":
                    require(item.get("mode") == "040000", "Unsupported chart directory mode")
                    continue
                self.blob(item)
                files[chart + "/" + name] = ("deploy/helm/" + chart + "/" + name, item)
            require(chart + "/Chart.yaml" in files and chart + "/values.yaml" in files,
                    "Missing Chart.yaml or values.yaml: " + chart)
        require(len(files) <= 256 and sum(item[1]["size"] for item in files.values()) <= MAX_TOTAL,
                "Chart acquisition exceeds the allowlist size budget")
        contents = {}
        for destination, (source, item) in sorted(files.items()):
            managed_chart_file(destination)
            raw, _ = self.http.get("https://raw.githubusercontent.com/" + REPOSITORY + "/" + sha + "/" +
                                   urllib.parse.quote(source, safe="/"))
            require(len(raw) == item["size"] and git_blob_sha(raw) == item["sha"],
                    "Git blob verification failed: " + destination)
            contents[destination] = raw
        versions = {}
        for chart in CHARTS:
            metadata = load_yaml(contents[chart + "/Chart.yaml"].decode(), "Chart.yaml")
            require(metadata.get("name") == chart and metadata.get("apiVersion") == "v2",
                    "Unexpected chart identity: " + chart)
            # The managed charts are self-contained. New dependencies require manual review.
            require(not metadata.get("dependencies"), "Chart dependencies require manual review: " + chart)
            version = metadata.get("version")
            require(isinstance(version, str) and VERSION.fullmatch(version), "Invalid chart version: " + chart)
            versions[chart] = version
        return sha, contents, versions


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        require(isinstance(key, (str, int)) and key not in result, "Duplicate or complex YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_yaml(text, context):
    try:
        result = yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError:
        raise UpdateError(context + ": invalid YAML") from None
    require(isinstance(result, dict), context + ": expected YAML mapping")
    return result


def yaml_node(text, path):
    node = yaml.compose(text)
    for part in path:
        if isinstance(node, yaml.MappingNode):
            found = [v for k, v in node.value if k.value == str(part)]
            require(len(found) == 1, "Cannot locate unique YAML field: " + str(path))
            node = found[0]
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            node = node.value[part]
        else:
            raise UpdateError("Unexpected YAML structure at " + str(path))
    return node


def scalar_edit(text, path, value):
    node = yaml_node(text, path)
    require(isinstance(node, yaml.ScalarNode) and node.style not in {"|", ">"}, "Expected simple YAML scalar")
    encoded = json.dumps(value) if node.style == '"' else ("'" + value.replace("'", "''") + "'" if node.style == "'" else value)
    return node.start_mark.index, node.end_mark.index, encoded


def edit_text(text, edits):
    edits = sorted(set(edits), reverse=True)
    end = len(text)
    for start, stop, value in edits:
        require(0 <= start < stop <= end, "Overlapping YAML text edits")
        text = text[:start] + value + text[stop:]
        end = start
    return text


def release_file(root, release):
    require(re.fullmatch(r"fluxer-[a-z-]+", release), "Invalid HelmRelease name")
    return local_path(root, APP / ("helmrelease-" + release.removeprefix("fluxer-") + ".yaml"))


def chart_release_edits(root, versions):
    changes = {}
    contracts = decode_json(local_path(root, CONTRACT).read_bytes(), "Deployment contract")
    for release in contracts:
        path = release_file(root, release)
        text = path.read_text()
        hr = load_yaml(text, release)
        chart_path = hr["spec"]["chart"]["spec"]["chart"]
        chart = Path(chart_path).name
        require(chart in CHARTS and chart_path == "./charts/fluxer/current/" + chart, "Unexpected chart path")
        current = hr["spec"]["chart"]["spec"]["version"]
        version = versions[chart]
        if current == version:
            continue
        edits = [scalar_edit(text, ("spec", "chart", "spec", "version"), version)]
        found = 0
        for r, renderer in enumerate(hr["spec"].get("postRenderers", [])):
            for p, patch in enumerate(renderer["kustomize"].get("patches", [])):
                operations = yaml.safe_load(patch["patch"])
                if not isinstance(operations, list):
                    continue
                for op in operations:
                    if op.get("path") != "/spec/template/metadata/labels/helm.sh~1chart":
                        continue
                    require(op.get("op") in {"replace", "add"} and
                            op.get("value") == (chart + "-" + current).replace("+", "_"),
                            "Unexpected chart label stabilization patch")
                    node = yaml_node(text, ("spec", "postRenderers", r, "kustomize", "patches", p, "patch"))
                    require(node.style == "|", "Chart label patch must use a literal YAML block")
                    original = text[node.start_mark.index:node.end_mark.index]
                    pattern = r"(?m)^(\s+value:\s*)" + re.escape(op["value"]) + r"(\s*(?:#.*)?)$"
                    updated, count = re.subn(pattern, lambda m: m[1] + (chart + "-" + version).replace("+", "_") + m[2], original)
                    require(count == 1, "Cannot uniquely update chart label patch")
                    edits.append((node.start_mark.index, node.end_mark.index, updated))
                    found += 1
        require(found >= 1, "Missing chart label stabilization patch: " + release)
        changes[path.relative_to(root)] = edit_text(text, edits).encode()
    return changes


def local_charts(root):
    base = local_path(root, CHART_ROOT / "UPSTREAM.json")
    existing = decode_json(base.read_bytes(), "UPSTREAM.json")
    require(existing.get("repository") == "https://github.com/" + REPOSITORY, "Unexpected upstream repository")
    old = {}
    for name, digest in existing["files"].items():
        managed_chart_file(name)
        content = local_path(root, CHART_ROOT / name).read_bytes()
        require(hashlib.sha256(content).hexdigest() == digest, "Local chart integrity check failed: " + name)
        old[name] = content
    actual = {"LICENSE"}
    for chart in CHARTS:
        directory = root / CHART_ROOT / chart
        require(directory.is_dir() and not directory.is_symlink(), "Missing or unsafe local chart directory")
        for path in directory.rglob("*"):
            require(not path.is_symlink(), "Symlink in local chart directory")
            if path.is_file():
                actual.add(path.relative_to(root / CHART_ROOT).as_posix())
    require(actual == set(old), "Local chart file inventory differs from UPSTREAM.json")
    return existing, old


def chart_plan(root, http, ref="main", verify=False):
    existing, old = local_charts(root)
    if verify:
        ref = existing.get("commit", "")
        require(isinstance(ref, str) and SHA1.fullmatch(ref), "Invalid pinned upstream commit")
    commit, contents, versions = GitHub(http).charts(ref)
    if verify:
        require(old == contents, "Vendored files do not exactly match the pinned upstream commit")
        return {}, ["# Fluxer upstream verification", "", "All allowed chart files and LICENSE exactly match immutable upstream commit `" + commit + "`."]
    changes = {CHART_ROOT / name: content for name, content in contents.items() if old.get(name) != content}
    changes.update({CHART_ROOT / name: None for name in old.keys() - contents.keys()})
    lines = ["# Fluxer chart update", "", "Upstream: https://github.com/" + REPOSITORY + "/commit/" + commit, ""]
    if changes:
        metadata = {**existing, "commit": commit, "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())}}
        changes[CHART_ROOT / "UPSTREAM.json"] = json_bytes(metadata)
        changes.update(chart_release_edits(root, versions))
        lines += ["Verified immutable Git tree entries and Git blob hashes for the five allowed charts and LICENSE.",
                  "Legacy charts, local policy files and historical deployment contracts are unchanged."]
    else:
        lines += ["No chart content changed. A newer upstream commit alone does not create a PR."]
    lines += ["", "CI validates rendered deployment contracts; it does not prove application startup or runtime compatibility."]
    return changes, lines


class Registry:
    def __init__(self, http):
        self.http = http

    @staticmethod
    def verify(raw, headers, expected=None, size=None):
        actual = sha256(raw)
        advertised = headers.get("docker-content-digest")
        require(expected or advertised, "Registry response has no verifiable digest")
        require(not expected or SHA256.fullmatch(expected) and expected == actual, "Registry digest mismatch")
        require(not advertised or SHA256.fullmatch(advertised) and advertised == actual, "Registry advertised digest mismatch")
        require(size is None or isinstance(size, int) and size == len(raw), "Registry descriptor size mismatch")
        return actual

    def image(self, repository, reference="v1"):
        require(re.fullmatch(r"ghcr\.io/fluxerapp/fluxer-[a-z-]+", repository), "Image repository outside allowlist")
        name = repository.removeprefix("ghcr.io/")
        query = urllib.parse.urlencode({"service": "ghcr.io", "scope": "repository:" + name + ":pull"})
        raw, _ = self.http.get("https://ghcr.io/token?" + query)
        response = decode_json(raw, "Public registry token")
        token = response.get("token") or response.get("access_token")
        require(isinstance(token, str) and token and "\n" not in token and "\r" not in token, "Missing registry token")
        headers = {"Authorization": "Bearer " + token, "Accept": ACCEPT}
        prefix = "https://ghcr.io/v2/" + name
        require(reference == "v1" or SHA256.fullmatch(reference), "Unsupported registry reference")
        raw, received = self.http.get(prefix + "/manifests/" + reference, headers)
        digest = self.verify(raw, received, reference if reference != "v1" else None)
        manifest = decode_json(raw, "Image manifest")
        require(manifest.get("schemaVersion") == 2, "Unsupported image manifest schema")
        if manifest.get("mediaType") in INDEX_TYPES:
            candidates = [d for d in manifest.get("manifests", []) if d.get("platform", {}).get("os") == "linux"
                          and d.get("platform", {}).get("architecture") == "amd64"
                          and not d.get("platform", {}).get("variant")]
            require(len(candidates) == 1, "Image index must have exactly one linux/amd64 manifest")
            descriptor = candidates[0]
            require(descriptor.get("mediaType") in MANIFEST_TYPES and SHA256.fullmatch(descriptor.get("digest", "")),
                    "Invalid platform manifest descriptor")
            require(isinstance(descriptor.get("size"), int), "Missing manifest descriptor size")
            raw, received = self.http.get(prefix + "/manifests/" + descriptor["digest"], headers)
            digest = self.verify(raw, received, descriptor["digest"], descriptor["size"])
            manifest = decode_json(raw, "Platform image manifest")
        require(manifest.get("schemaVersion") == 2 and manifest.get("mediaType") in MANIFEST_TYPES,
                "Unsupported platform image manifest")
        descriptor = manifest.get("config", {})
        require(descriptor.get("mediaType") in CONFIG_TYPES and SHA256.fullmatch(descriptor.get("digest", ""))
                and isinstance(descriptor.get("size"), int),
                "Invalid image config descriptor")
        raw, received = self.http.get(prefix + "/blobs/" + descriptor["digest"], headers)
        self.verify(raw, received, descriptor["digest"], descriptor["size"])
        config = decode_json(raw, "Image config")
        require(config.get("os") == "linux" and config.get("architecture") == "amd64" and not config.get("variant"),
                "Image config platform is not linux/amd64")
        settings = config.get("config") or {}
        labels = settings.get("Labels") or {}
        revision = labels.get("org.opencontainers.image.revision")
        runtime = {"entrypoint": settings.get("Entrypoint"), "command": settings.get("Cmd"),
                   "working_dir": settings.get("WorkingDir"), "user": settings.get("User")}
        for field in ("entrypoint", "command"):
            require(runtime[field] is None or isinstance(runtime[field], list) and len(runtime[field]) <= 64
                    and all(isinstance(arg, str) and len(arg) <= 2048 for arg in runtime[field]),
                    "Unsupported OCI startup metadata")
        for field in ("working_dir", "user"):
            require(runtime[field] is None or isinstance(runtime[field], str) and len(runtime[field]) <= 2048,
                    "Unsupported OCI startup metadata")
        for entry in settings.get("Env") or []:
            require(isinstance(entry, str), "Invalid OCI environment metadata")
            if entry.startswith("BUILD_VERSION="):
                runtime["build_version"] = entry.partition("=")[2]
        require(len(json.dumps(runtime)) <= 8192, "OCI startup metadata exceeds the size limit")
        result = {"repository": repository, "tag": "v1", "digest": digest,
                  "config_descriptor": {"digest": descriptor["digest"], "size": descriptor["size"]},
                  "runtime": runtime}
        if isinstance(revision, str) and SHA1.fullmatch(revision):
            result["source_revision"] = revision
        return result


def load_lock(root):
    lock = decode_json(local_path(root, LOCK).read_bytes(), "Image lock")
    require(lock.get("schema_version") == 1 and lock.get("channel") == "v1" and lock.get("platform") == "linux/amd64",
            "Unsupported image lock policy")
    contracts = decode_json(local_path(root, CONTRACT).read_bytes(), "Deployment contract")
    require(set(lock["releases"]) == set(contracts), "Image lock release inventory changed")
    for release, workloads in lock["releases"].items():
        expected = contracts[release]["workloads"]
        require(set(workloads) == set(expected), "Image lock workload inventory changed")
        for workload, containers in workloads.items():
            require(set(containers) == set(expected[workload]["images"]), "Image lock container inventory changed")
            for container, record in containers.items():
                require(record.get("repository") == expected[workload]["repositories"][container] and
                        record.get("tag") == "v1" and SHA256.fullmatch(record.get("digest", "")),
                        "Image lock repository, channel or digest invalid")
    return lock


def image_value_path(chart, workload):
    kind, name = workload.split("/", 1)
    if chart == "fluxer-gateway":
        return ("deployments" if kind == "Deployment" else "statefulsets", name, "image")
    if chart == "fluxer-api":
        return ("api", name, "image")
    if chart == "fluxer-svc":
        return ("services", name.removesuffix("-shard"), "image")
    require(chart in {"fluxer-web", "fluxer-media-proxy"}, "Unknown image values layout")
    return ("workloads", name, "image")


def image_release_edits(root, lock, candidates=None):
    changes = {}
    for release, workloads in lock["releases"].items():
        file = release_file(root, release)
        text = file.read_text()
        hr = load_yaml(text, release)
        chart = Path(hr["spec"]["chart"]["spec"]["chart"]).name
        values = hr["spec"]["values"]
        edits = []
        for workload, containers in workloads.items():
            require(len(containers) == 1, "Image updater requires one managed container per workload")
            record = next(iter(containers.values()))
            path = image_value_path(chart, workload)
            value = values
            for part in path:
                value = value[part]
            repository = value.get("repository") or values["image"]["registry"] + "/" + value["name"]
            require(repository == record["repository"] and value["digest"] == record["digest"] and
                    value.get("tag", values["image"]["tag"]) == record["tag"],
                    "HelmRelease image differs from image lock: " + release + "/" + workload)
            if candidates is None:
                continue
            digest = candidates[repository]["digest"]
            if digest != record["digest"]:
                edits.append(scalar_edit(text, ("spec", "values") + path + ("digest",), digest))
        if edits:
            changes[file.relative_to(root)] = edit_text(text, edits).encode()
    return changes


def image_plan(root, http, verify=False):
    lock = load_lock(root)
    records = [r for workloads in lock["releases"].values() for containers in workloads.values() for r in containers.values()]
    registry = Registry(http)
    if verify:
        verified = {}
        index_pins = set()
        for record in records:
            key = (record["repository"], record["digest"])
            if key not in verified:
                verified[key] = registry.image(*key)
            actual = verified[key]
            if actual["digest"] != record["digest"]:
                # Registry.image verifies the requested immutable index bytes
                # before selecting and verifying the amd64 child and config.
                # New candidates still use the resolved platform digest.
                index_pins.add(key)
            for field in ("source_revision", "config_descriptor", "runtime"):
                if field in record:
                    require(record[field] == actual.get(field), "Image lock metadata differs from immutable registry content")
        # Also check text bindings without proposing a newer tag snapshot.
        image_release_edits(root, lock)
        return {}, ["# Fluxer image verification", "", str(len(verified)) + " immutable image manifests and configs verified for linux/amd64.",
                    str(len(index_pins)) + " immutable OCI index pins were resolved to and verified through their linux/amd64 child manifests.",
                    "The current v1 tag is intentionally not compared. Image metadata does not prove application startup or operation."]
    candidates = {repo: registry.image(repo) for repo in sorted({r["repository"] for r in records})}
    changes = image_release_edits(root, lock, candidates)
    changed_repos = {r["repository"] for r in records if r["digest"] != candidates[r["repository"]]["digest"]}
    if changed_repos:
        updated = copy.deepcopy(lock)
        for workloads in updated["releases"].values():
            for containers in workloads.values():
                for name, record in containers.items():
                    if record["repository"] in changed_repos:
                        containers[name] = candidates[record["repository"]]
        changes[LOCK] = json_bytes(updated)
    lines = ["# Fluxer image update", "", "Channel: `v1`; platform: `linux/amd64`.", "",
             "Index/manifest/config bytes were verified against their SHA-256 digests. Image layers were not downloaded or executed.",
             "These are independent component tag snapshots, not an atomic release image set.",
             "CI checks manifests and metadata; it does not prove application startup, database compatibility, or end-to-end operation.", ""]
    revisions = {r.get("source_revision") for r in candidates.values()}
    if len(revisions - {None}) > 1:
        lines += ["**Component source revisions are mixed. Review compatibility before merging.**", ""]
    if None in revisions:
        lines += ["Some components have no valid OCI source revision label.", ""]
    if not changed_repos:
        lines += ["No platform image digest changed; existing lock metadata is retained.", ""]
    for repo, candidate in candidates.items():
        old = sorted({r["digest"] for r in records if r["repository"] == repo})
        lines += ["## " + repo, "", "- Previous: " + ", ".join("`" + d + "`" for d in old),
                  "- Candidate: `" + candidate["digest"] + "`",
                  "- Source revision: `" + candidate.get("source_revision", "unknown") + "`",
                  "- Config digest: `" + candidate["config_descriptor"]["digest"] + "`"]
        # Quote bounded metadata as JSON; do not copy arbitrary image environment variables.
        runtime = json.dumps(candidate["runtime"], ensure_ascii=True).replace("<", "\\u003c").replace("`", "\\u0060")
        lines += ["- OCI startup metadata: `" + runtime[:2000] + "`", ""]
        if repo.endswith("/fluxer-api"):
            lines += ["**API startup needs manual validation:** the deployment overrides the image command with a TypeScript/tsx NATS wrapper. "
                      "OCI config cannot establish that tsx, source paths, or Config.nats still exist in the image.", ""]
    return changes, lines


def apply_changes(root, changes):
    """Stage old and new files; publish and roll back using atomic replacements."""
    targets = {relative: local_path(root, relative) for relative in changes}
    originals = {relative: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
                 for relative, path in targets.items()}
    staging = Path(tempfile.mkdtemp(prefix=".fluxer-update-", dir=root))
    retain_recovery = False
    try:
        backup_root = staging / "originals"
        backup_root.mkdir()
        for relative, original in originals.items():
            if original is not None:
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(original[0])
                backup.chmod(original[1])
        for relative, content in changes.items():
            if content is not None:
                staged = staging / "updates" / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(content)
                staged.chmod(originals[relative][1] if originals[relative] else 0o644)
        applied = []
        try:
            for relative, content in sorted(changes.items()):
                path = targets[relative]
                if content is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staging / "updates" / relative, path)
                applied.append(relative)
        except OSError:
            failures = []
            for relative in reversed(applied):
                try:
                    if originals[relative] is None:
                        targets[relative].unlink(missing_ok=True)
                    else:
                        # Directory permissions govern replacement, so this also
                        # restores files whose original/current mode is 0444.
                        os.replace(backup_root / relative, targets[relative])
                except OSError:
                    failures.append(str(relative))
            if failures:
                # Keep failed originals available for manual recovery rather
                # than deleting their only on-disk copy on context-manager exit.
                retain_recovery = True
                raise UpdateError("Could not publish staged update; rollback failed for: " +
                                  ", ".join(failures) + ". Recovery files retained at: " +
                                  str(backup_root)) from None
            raise UpdateError("Could not publish staged update; original files restored") from None
    finally:
        if not retain_recovery:
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=("charts", "images"))
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--upstream-ref", default="main", help="Chart branch, tag or immutable commit (default: main)")
    parser.add_argument("--verify-upstream", action="store_true", help="Require local charts to exactly match their pinned upstream commit; never write updates")
    parser.add_argument("--verify-images", action="store_true", help="Verify locked image digests, metadata and platform against the registry; never track the moving tag")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        if args.report and (args.check_only or args.verify_upstream or args.verify_images):
            require(not args.report.resolve().is_relative_to(root), "Read-only modes require --report outside the repository")
        if args.kind == "images":
            require(args.upstream_ref == "main", "--upstream-ref only applies to charts")
        require(not args.verify_upstream or args.kind == "charts", "--verify-upstream requires --kind charts")
        require(not args.verify_images or args.kind == "images", "--verify-images requires --kind images")
        http = HTTP()
        changes, lines = (chart_plan(root, http, args.upstream_ref, args.verify_upstream) if args.kind == "charts"
                          else image_plan(root, http, args.verify_images))
        if args.check_only:
            lines += ["", "Check only: no update was written to the repository."]
        elif changes:
            apply_changes(root, changes)
        lines += ["", "Changed files: " + str(len(changes)), ""]
        lines += ["- " + ("Delete " if content is None else "Update ") + "`" + str(path) + "`"
                  for path, content in sorted(changes.items())]
        report = "\n".join(lines) + "\n"
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(report)
        print(args.kind + ": " + str(len(changes)) + " candidate file changes" + (" (check only)" if args.check_only else ""))
        return 0
    except (UpdateError, KeyError, TypeError, UnicodeError, OSError) as error:
        # Unexpected structural exceptions expose no upstream response or credentials.
        message = str(error) if isinstance(error, UpdateError) else "Invalid local or upstream structure / file access failure"
        print("Fluxer update failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
