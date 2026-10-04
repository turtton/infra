#!/usr/bin/env python3
"""Execute publisher branch guards against isolated local Git repositories.

Run: uv run --with pyyaml==6.0.3 python tests/scripts/test-fluxer-update-workflow.py
Git and jq are real; gh is an offline fixture. No project refs are modified.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/fluxer-update-kind.yml"
GIT = shutil.which("git")
BOT = "github-actions[bot]"
BRANCH = "codex/fluxer-images-updates"
HR_PATH = "clusters/main/apps/fluxer/helmrelease-api.yaml"

GH_MOCK = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  'pr list')
    if [ "$GH_MOCK_EXISTING" = true ] || [ -f "$GH_MOCK_CREATED" ]; then echo 17; fi ;;
  'pr view')
    if [[ "$*" == *'--json body'* ]]; then
      if [ "$GH_MOCK_RECOVERY_VIEW" = failure ]; then exit 1; fi
      if [ -n "$GH_MOCK_RECOVERY_HOOK" ]; then bash "$GH_MOCK_RECOVERY_HOOK" >&2; fi
    fi
    if [[ "$*" == *'--json headRefOid --jq'* ]]; then
      "$REAL_GIT" --git-dir="$GH_MOCK_REMOTE" rev-parse "refs/heads/$UPDATE_BRANCH"
    elif [[ "$*" == *'--json headRefOid,isDraft'* ]]; then
      head="$("$REAL_GIT" --git-dir="$GH_MOCK_REMOTE" rev-parse "refs/heads/$UPDATE_BRANCH")"
      jq --arg head "$head" '.headRefOid=$head | .isDraft=(.isDraft // false)' "$GH_MOCK_PR"
    else cat "$GH_MOCK_PR"; fi ;;
  'api repos/'*)
    if [[ "$2" == *'/commits/'* ]]; then echo "$GH_MOCK_COMMITTER"
    elif [[ "$2" == *'/git/ref/heads/'* ]]; then
      if [ "$GH_MOCK_BRANCH" = true ]; then echo '{}'; else exit 1; fi
    else echo "Unexpected gh api: $*" >&2; exit 99; fi ;;
  'pr edit'|'pr create')
    printf '%s\n' "$*" >> "$GH_MOCK_MUTATIONS"
    operation="$2"
    result="$GH_MOCK_EDIT_RESULT"
    if [ "$operation" = create ]; then result="$GH_MOCK_CREATE_RESULT"; fi
    if [ "$result" = failure ]; then exit 1; fi
    body=""
    draft=false
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --body-file) body="$2"; shift ;;
        --draft) draft=true ;;
      esac
      shift
    done
    head="$("$REAL_GIT" --git-dir="$GH_MOCK_REMOTE" rev-parse "refs/heads/$UPDATE_BRANCH")"
    if [ "$operation" = create ]; then
      jq -n --arg head "$head" --rawfile body "$body" --argjson draft "$draft" \
        '{headRefOid:$head,body:$body,isDraft:$draft,commits:[{oid:$head,authors:[{login:"github-actions[bot]"}]}]}' > "$GH_MOCK_PR"
      touch "$GH_MOCK_CREATED"
    else
      jq --arg head "$head" --rawfile body "$body" \
        '.headRefOid=$head | .body=$body | .commits=[{oid:$head,authors:[{login:"github-actions[bot]"}]}]' \
        "$GH_MOCK_PR" > "$GH_MOCK_PR.tmp"
      mv "$GH_MOCK_PR.tmp" "$GH_MOCK_PR"
    fi
    if [ "$result" = unknown-success ]; then exit 1; fi ;;
  'pr ready')
    printf '%s\n' "$*" >> "$GH_MOCK_MUTATIONS"
    if [ "$GH_MOCK_READY_RESULT" = failure ]; then exit 1; fi
    draft=false
    if [[ "$*" == *--undo* ]]; then draft=true; fi
    jq --argjson draft "$draft" '.isDraft=$draft' "$GH_MOCK_PR" > "$GH_MOCK_PR.tmp"
    mv "$GH_MOCK_PR.tmp" "$GH_MOCK_PR" ;;
  'run list')
    workflow=""
    event=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --workflow ]; then workflow="$2"; shift; fi
      if [ "$1" = --event ]; then event="$2"; shift; fi
      shift
    done
    jq --arg workflow "$workflow" --arg event "$event" \
      '(.[$workflow] // []) | map(select($event == "" or (.event // "workflow_dispatch") == $event))' "$GH_MOCK_RUNS" ;;
  'workflow run')
    printf '%s\n' "$*" >> "$GH_MOCK_MUTATIONS"
    jq --arg workflow "$3" '.[$workflow]=([{status:"queued",conclusion:null}]+(.[$workflow] // []))' \
      "$GH_MOCK_RUNS" > "$GH_MOCK_RUNS.tmp"
    mv "$GH_MOCK_RUNS.tmp" "$GH_MOCK_RUNS" ;;
  *) echo "Unexpected gh call: $*" >&2; exit 99 ;;
esac
'''

GIT_WRAPPER = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$GIT_MOCK_LOG"
exec "$REAL_GIT" "$@"
'''


class PublisherWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        steps = workflow["jobs"]["publish"]["steps"]
        cls.guard_script = next(s["run"] for s in steps
                                if s.get("name") == "Check candidate and preserve manual branch edits")
        cls.publish_script = next(s["run"] for s in steps
                                  if s.get("name") == "Create or refresh update PR")
        cls.dispatch_script = next(s["run"] for s in steps
                                   if s.get("name") == "Run validation on the proposed commit")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fluxer-publisher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.repo = self.root / "checkout"
        self.remote = self.root / "remote.git"
        self.runner = self.root / "runner"
        self.artifacts = self.runner / "fluxer-proposal"
        self.artifacts.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, script in [("gh", GH_MOCK), ("git", GIT_WRAPPER)]:
            executable = self.bin / name
            executable.write_text(script)
            executable.chmod(0o755)
        self.git("init", "--bare", str(self.remote), cwd=self.root)
        self.git("init", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.name", BOT)
        self.git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        self.git("config", "commit.gpgsign", "false")
        self.write(HR_PATH, "baseline\n")
        self.write("clusters/main/apps/fluxer/images.lock.json", "{}\n")
        self.write("charts/fluxer/current/fluxer-api/values.yaml", "baseline\n")
        self.write("README.md", "baseline\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial main")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + self.env["PATH"],
            "REAL_GIT": GIT,
            "RUNNER_TEMP": str(self.runner),
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
            "GH_REPO": "fixture/infra",
            "UPDATE_KIND": "images",
            "UPDATE_BRANCH": BRANCH,
            "GH_MOCK_PR": str(self.root / "pr.json"),
            "GH_MOCK_LOG": str(self.root / "gh.log"),
            "GH_MOCK_MUTATIONS": str(self.root / "gh-mutations.log"),
            "GH_MOCK_EXISTING": "false",
            "GH_MOCK_BRANCH": "false",
            "GH_MOCK_COMMITTER": BOT,
            "GH_MOCK_REMOTE": str(self.remote),
            "GH_MOCK_CREATED": str(self.root / "created-pr"),
            "GH_MOCK_EDIT_RESULT": "success",
            "GH_MOCK_CREATE_RESULT": "success",
            "GH_MOCK_READY_RESULT": "success",
            "GH_MOCK_RECOVERY_VIEW": "success",
            "GH_MOCK_RECOVERY_HOOK": "",
            "GH_MOCK_RUNS": str(self.root / "runs.json"),
            "GIT_MOCK_LOG": str(self.root / "git.log"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        })
        (self.artifacts / "report.md").write_text("Fixture report\n")
        pathlib.Path(self.env["GH_MOCK_RUNS"]).write_text("{}")
        self.make_candidate()

    def git(self, *args, cwd=None):
        environment = os.environ.copy()
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.check_output([GIT, *args], cwd=cwd or self.repo, env=environment,
                                       text=True, stderr=subprocess.PIPE)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def make_candidate(self, path=HR_PATH, content="candidate\n", **metadata):
        self.write(path, content)
        self.git("add", ".")
        patch = self.git("diff", "--cached", "--binary")
        (self.artifacts / "candidate.patch").write_text(patch)
        self.git("reset", "--hard", "HEAD")
        data = {"kind": "images", "base_sha": self.base, "changed": True, "compatible": True}
        data.update(metadata)
        (self.artifacts / "metadata.json").write_text(json.dumps(data))

    def make_existing(self, content="previous proposal\n", author=BOT, recorded_head=None, authors=None):
        self.git("checkout", "-b", BRANCH)
        self.write(HR_PATH, content)
        self.git("add", ".")
        self.git("commit", "-m", "existing proposal")
        head = self.git("rev-parse", "HEAD").strip()
        self.git("push", "origin", BRANCH)
        self.git("checkout", "main")
        self.git("branch", "-D", BRANCH)
        self.env["GH_MOCK_EXISTING"] = "true"
        self.env["GH_MOCK_BRANCH"] = "true"
        pr = {"headRefOid": head, "body": "Fixture report\n<!-- fluxer-update-head:" +
              (recorded_head or head) + " -->\n",
              "isDraft": False,
              "commits": [{"oid": head, "authors": authors if authors is not None else [{"login": author}]}]}
        pathlib.Path(self.env["GH_MOCK_PR"]).write_text(json.dumps(pr))
        return head

    def run_script(self, script):
        return subprocess.run(["bash", "-c", script], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def guard(self):
        return self.run_script(self.guard_script)

    def publish(self):
        output = self.outputs()
        self.env.update({"CHECKED_HEAD": output.get("checked_head", ""),
                         "EXISTING_PR": output.get("existing_pr", ""),
                         "COMPATIBLE": output.get("compatible", "")})
        return self.run_script(self.publish_script)

    def dispatch(self):
        output = self.outputs()
        self.env.update({"PR_NUMBER": output["pull-request-number"],
                         "EXPECTED_SHA": output["pull-request-head-sha"]})
        return self.run_script(self.dispatch_script)

    def outputs(self):
        path = pathlib.Path(self.env["GITHUB_OUTPUT"])
        return dict(line.split("=", 1) for line in path.read_text().splitlines()) if path.exists() else {}

    def log(self, variable):
        path = pathlib.Path(self.env[variable])
        return path.read_text().splitlines() if path.exists() else []

    def remote_head(self):
        return self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/" + BRANCH,
                        cwd=self.root).strip()

    def retry_from_main(self):
        self.git("checkout", "main")
        self.git("reset", "--hard", "HEAD")
        branches = self.git("branch", "--list", BRANCH).strip()
        if branches:
            self.git("branch", "-D", BRANCH)
        pathlib.Path(self.env["GITHUB_OUTPUT"]).write_text("")
        (self.artifacts / "report.md").write_text("Fixture report\n")
        self.make_candidate()

    def set_draft(self, draft):
        path = pathlib.Path(self.env["GH_MOCK_PR"])
        data = json.loads(path.read_text())
        data["isDraft"] = draft
        path.write_text(json.dumps(data))

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_rejected_before_apply(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), [])

    def test_recorded_bot_head_and_bot_committer_are_accepted(self):
        head = self.make_existing()
        self.assert_success(self.guard())
        self.assertEqual(self.outputs()["checked_head"], head)
        self.assertEqual(self.outputs()["existing_pr"], "17")
        self.assertEqual(self.git("show", ":" + HR_PATH), "candidate\n")
        self.assertTrue(any("commits/" + head in call for call in self.log("GH_MOCK_LOG")))

    def test_amended_head_marker_mismatch_is_rejected(self):
        head = self.make_existing(recorded_head="b" * 40)
        self.assert_rejected_before_apply(self.guard(), "branch changed after automation published it")
        self.assertEqual(self.remote_head(), head)

    def test_human_committer_is_rejected_even_with_bot_author(self):
        self.make_existing()
        self.env["GH_MOCK_COMMITTER"] = "human"
        self.assert_rejected_before_apply(self.guard(), "A human amended the update branch")

    def test_human_author_and_missing_authors_are_rejected(self):
        self.make_existing(author="human")
        self.assert_rejected_before_apply(self.guard(), "contains manual commits")
        pr = json.loads(pathlib.Path(self.env["GH_MOCK_PR"]).read_text())
        pr["commits"][0]["authors"] = []
        pathlib.Path(self.env["GH_MOCK_PR"]).write_text(json.dumps(pr))
        self.assert_rejected_before_apply(self.guard(), "contains manual commits")

    def test_branch_without_open_automation_pr_is_preserved(self):
        self.make_existing()
        self.env["GH_MOCK_EXISTING"] = "false"
        self.assert_rejected_before_apply(self.guard(), "existing branch has no open automation PR")

    def test_stale_main_proposal_is_skipped(self):
        self.make_candidate(base_sha="c" * 40)
        self.assert_success(self.guard())
        self.assertEqual(self.outputs(), {"changed": "false"})
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.log("GH_MOCK_LOG"), [])

    def test_no_change_proposal_is_skipped(self):
        self.make_candidate(changed=False)
        self.assert_success(self.guard())
        self.assertEqual(self.outputs()["changed"], "false")
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.log("GH_MOCK_LOG"), [])

    def test_wrong_kind_and_invalid_booleans_are_rejected(self):
        for metadata in [{"kind": "charts"}, {"changed": "maybe"}, {"compatible": "maybe"}]:
            with self.subTest(metadata=metadata):
                self.make_candidate(**metadata)
                self.assertNotEqual(self.guard().returncode, 0)
                self.assertEqual(self.git("diff", "--cached"), "")
                self.assertEqual(self.log("GH_MOCK_LOG"), [])

    def test_unexpected_patch_path_is_rejected(self):
        self.make_candidate(path="README.md")
        result = self.guard()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected proposal path: README.md", result.stdout)
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), [])

    def test_charts_patch_cannot_be_published_as_image_update(self):
        self.make_candidate(path="charts/fluxer/current/fluxer-api/values.yaml")
        self.assertNotEqual(self.guard().returncode, 0)
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), [])

    def test_image_lock_patch_cannot_be_published_as_chart_update(self):
        self.env["UPDATE_KIND"] = "charts"
        self.make_candidate(path="clusters/main/apps/fluxer/images.lock.json", content='{"changed":true}\n',
                            kind="charts")
        self.assertNotEqual(self.guard().returncode, 0)
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), [])

    def test_same_candidate_tree_does_not_push_or_modify_pr(self):
        head = self.make_existing(content="candidate\n")
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        self.assertEqual(self.remote_head(), head)
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), [])
        self.assertFalse(any(" push " in " " + call + " " for call in self.log("GIT_MOCK_LOG")))
        self.assertFalse(any(call.startswith("commit ") for call in self.log("GIT_MOCK_LOG")))
        self.assertEqual(self.outputs()["pull-request-number"], "17")
        self.assertEqual(self.outputs()["pull-request-head-sha"], head)

    def test_changed_candidate_push_lease_is_bound_to_inspected_head(self):
        head = self.make_existing()
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        pushes = [call for call in self.log("GIT_MOCK_LOG") if " push " in " " + call + " "]
        self.assertEqual(len(pushes), 1)
        self.assertIn("--force-with-lease=refs/heads/" + BRANCH + ":" + head, pushes[0])
        self.assertNotEqual(self.remote_head(), head)
        self.assertEqual(self.remote_head(), self.outputs()["pull-request-head-sha"])
        self.assertEqual(len(self.log("GH_MOCK_MUTATIONS")), 1)
        self.assertTrue(self.log("GH_MOCK_MUTATIONS")[0].startswith("pr edit 17 "))

    def test_failed_pr_edit_restores_inspected_head_and_next_guard_can_retry(self):
        head = self.make_existing()
        self.env["GH_MOCK_EDIT_RESULT"] = "failure"
        self.assert_success(self.guard())
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restored the branch after incomplete PR publication", result.stdout)
        self.assertEqual(self.remote_head(), head)
        pushes = [call for call in self.log("GIT_MOCK_LOG") if " push " in " " + call + " "]
        self.assertEqual(len(pushes), 2)
        new_head = self.git("rev-parse", "HEAD").strip()
        self.assertIn("--force-with-lease=refs/heads/" + BRANCH + ":" + new_head, pushes[1])
        self.assertIn(head + ":refs/heads/" + BRANCH, pushes[1])
        self.retry_from_main()
        self.assert_success(self.guard())

    def test_failed_pr_creation_deletes_only_the_pushed_head(self):
        self.env["GH_MOCK_CREATE_RESULT"] = "failure"
        self.assert_success(self.guard())
        self.assertNotEqual(self.publish().returncode, 0)
        new_head = self.git("rev-parse", "HEAD").strip()
        refs = self.git("--git-dir=" + str(self.remote), "for-each-ref", "--format=%(refname)",
                        "refs/heads/" + BRANCH, cwd=self.root)
        self.assertEqual(refs, "")
        pushes = [call for call in self.log("GIT_MOCK_LOG") if " push " in " " + call + " "]
        self.assertIn("--force-with-lease=refs/heads/" + BRANCH + ":" + new_head, pushes[-1])
        self.assertTrue(pushes[-1].endswith("origin :refs/heads/" + BRANCH))
        self.retry_from_main()
        self.assert_success(self.guard())

    def test_pr_edit_unknown_success_keeps_recorded_head_and_next_run_reuses_it(self):
        old_head = self.make_existing()
        self.env["GH_MOCK_EDIT_RESULT"] = "unknown-success"
        self.assert_success(self.guard())
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publication succeeded despite the API error", result.stdout)
        published_head = self.remote_head()
        self.assertNotEqual(published_head, old_head)
        self.assertIn(published_head, json.loads(pathlib.Path(self.env["GH_MOCK_PR"]).read_text())["body"])
        self.retry_from_main()
        self.env["GH_MOCK_EDIT_RESULT"] = "success"
        self.assert_success(self.guard())
        previous_log = self.log("GH_MOCK_MUTATIONS")
        self.assert_success(self.publish())
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), previous_log)
        self.assertEqual(self.outputs()["pull-request-head-sha"], published_head)

    def test_pr_creation_unknown_success_keeps_new_branch_and_pr(self):
        self.env["GH_MOCK_CREATE_RESULT"] = "unknown-success"
        self.assert_success(self.guard())
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publication succeeded despite the API error", result.stdout)
        published_head = self.remote_head()
        self.retry_from_main()
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        self.assertEqual(self.outputs()["pull-request-head-sha"], published_head)
        self.assertEqual(len(self.log("GH_MOCK_MUTATIONS")), 1)

    def test_unreadable_pr_outcome_preserves_branch_for_inspection(self):
        old_head = self.make_existing()
        self.env["GH_MOCK_EDIT_RESULT"] = "failure"
        self.env["GH_MOCK_RECOVERY_VIEW"] = "failure"
        self.assert_success(self.guard())
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot inspect PR publication outcome", result.stdout)
        self.assertNotEqual(self.remote_head(), old_head)
        self.assertEqual(self.remote_head(), self.git("rev-parse", "HEAD").strip())

    def test_cas_recovery_preserves_human_push_during_rollback(self):
        self.make_existing()
        self.assert_success(self.guard())
        human = self.root / "recovery-writer"
        self.git("clone", "--branch", BRANCH, str(self.remote), str(human), cwd=self.root)
        self.git("config", "user.name", "Human", cwd=human)
        self.git("config", "user.email", "human@example.invalid", cwd=human)
        self.git("config", "commit.gpgsign", "false", cwd=human)
        hook = self.root / "recovery-hook.sh"
        hook.write_text('''set -euo pipefail
"$REAL_GIT" -C "$HUMAN_REPO" fetch origin "$UPDATE_BRANCH"
"$REAL_GIT" -C "$HUMAN_REPO" reset --hard "origin/$UPDATE_BRANCH"
echo 'human correction' > "$HUMAN_REPO/clusters/main/apps/fluxer/helmrelease-api.yaml"
"$REAL_GIT" -C "$HUMAN_REPO" add .
"$REAL_GIT" -C "$HUMAN_REPO" commit -m 'manual correction during recovery'
"$REAL_GIT" -C "$HUMAN_REPO" push origin "$UPDATE_BRANCH"
''')
        self.env["HUMAN_REPO"] = str(human)
        self.env["GH_MOCK_RECOVERY_HOOK"] = str(hook)
        self.env["GH_MOCK_EDIT_RESULT"] = "failure"
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch advanced during recovery", result.stdout)
        human_head = self.git("rev-parse", "HEAD", cwd=human).strip()
        self.assertEqual(self.remote_head(), human_head)
        self.assertNotEqual(human_head, self.git("rev-parse", "HEAD").strip())

    def test_unchanged_candidate_repairs_draft_state_without_commit_or_pr_edit(self):
        head = self.make_existing(content="candidate\n")
        self.set_draft(True)
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), ["pr ready 17"])
        self.assertEqual(self.remote_head(), head)
        self.assertFalse(json.loads(pathlib.Path(self.env["GH_MOCK_PR"]).read_text())["isDraft"])
        self.assertEqual(self.outputs()["pull-request-head-sha"], head)
        self.assertFalse(any(call.startswith("commit ") for call in self.log("GIT_MOCK_LOG")))

    def test_unchanged_incompatible_candidate_is_restored_to_draft(self):
        head = self.make_existing(content="candidate\n")
        self.make_candidate(compatible=False)
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), ["pr ready 17 --undo"])
        self.assertEqual(self.remote_head(), head)
        self.assertTrue(json.loads(pathlib.Path(self.env["GH_MOCK_PR"]).read_text())["isDraft"])

    def test_draft_repair_failure_after_publication_keeps_recorded_head_for_retry(self):
        self.make_existing()
        self.set_draft(True)
        self.env["GH_MOCK_READY_RESULT"] = "failure"
        self.assert_success(self.guard())
        self.assertNotEqual(self.publish().returncode, 0)
        head = self.remote_head()
        self.assertIn(head, json.loads(pathlib.Path(self.env["GH_MOCK_PR"]).read_text())["body"])
        self.retry_from_main()
        self.env["GH_MOCK_READY_RESULT"] = "success"
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        self.assertEqual(self.outputs()["pull-request-head-sha"], head)
        edits = [call for call in self.log("GH_MOCK_MUTATIONS") if call.startswith("pr edit ")]
        self.assertEqual(len(edits), 1)

    def test_dispatch_reuses_successful_and_active_checks_on_unchanged_candidate(self):
        head = self.make_existing(content="candidate\n")
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        pathlib.Path(self.env["GH_MOCK_RUNS"]).write_text(json.dumps({
            "fluxer-chart-check.yml": [{"status": "completed", "conclusion": "success"}],
            "flux-check.yml": [{"status": "queued", "conclusion": None}]}))
        self.assert_success(self.dispatch())
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), [])
        lookups = [call for call in self.log("GH_MOCK_LOG") if call.startswith("run list ")]
        self.assertEqual(len(lookups), 2)
        self.assertTrue(all("--commit " + head in call for call in lookups))
        self.assertTrue(all("--branch " + BRANCH in call for call in lookups))

    def test_dispatch_retries_failed_or_missing_checks_once_without_pr_churn(self):
        head = self.make_existing(content="candidate\n")
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        pathlib.Path(self.env["GH_MOCK_RUNS"]).write_text(json.dumps({
            "fluxer-chart-check.yml": [{"status": "completed", "conclusion": "failure"}]}))
        self.assert_success(self.dispatch())
        calls = self.log("GH_MOCK_MUTATIONS")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.startswith("workflow run ") for call in calls))
        self.assertTrue(all("expected_sha=" + head in call for call in calls))
        self.assert_success(self.dispatch())
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), calls)
        self.assertEqual(self.remote_head(), head)

    def test_dispatch_retries_only_failed_workflow_and_preserves_in_progress_run(self):
        self.make_existing(content="candidate\n")
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        pathlib.Path(self.env["GH_MOCK_RUNS"]).write_text(json.dumps({
            "fluxer-chart-check.yml": [{"status": "completed", "conclusion": "failure"}],
            "flux-check.yml": [{"status": "in_progress", "conclusion": None}]}))
        self.assert_success(self.dispatch())
        calls = self.log("GH_MOCK_MUTATIONS")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("workflow run fluxer-chart-check.yml "))

    def test_waiting_pr_run_does_not_prevent_workflow_dispatch_validation(self):
        self.make_existing(content="candidate\n")
        self.assert_success(self.guard())
        self.assert_success(self.publish())
        pathlib.Path(self.env["GH_MOCK_RUNS"]).write_text(json.dumps({
            "fluxer-chart-check.yml": [{"status": "waiting", "conclusion": None,
                                        "event": "pull_request"}],
            "flux-check.yml": [{"status": "completed", "conclusion": "success",
                                 "event": "workflow_dispatch"}]}))
        self.assert_success(self.dispatch())
        calls = self.log("GH_MOCK_MUTATIONS")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("workflow run fluxer-chart-check.yml "))
        lookups = [call for call in self.log("GH_MOCK_LOG") if call.startswith("run list ")]
        self.assertTrue(all("--event workflow_dispatch" in call for call in lookups))

    def test_real_force_with_lease_rejects_human_push_after_inspection(self):
        head = self.make_existing()
        self.assert_success(self.guard())
        # An independent writer advances a real local bare remote after the
        # workflow has recorded CHECKED_HEAD. Fetching the old SHA must not
        # change the lease to a newer tracking ref.
        human = self.root / "human"
        self.git("clone", "--branch", BRANCH, str(self.remote), str(human), cwd=self.root)
        self.git("config", "user.name", "Human", cwd=human)
        self.git("config", "user.email", "human@example.invalid", cwd=human)
        self.git("config", "commit.gpgsign", "false", cwd=human)
        (human / HR_PATH).write_text("human correction\n")
        self.git("add", ".", cwd=human)
        self.git("commit", "-m", "manual correction", cwd=human)
        self.git("push", "origin", BRANCH, cwd=human)
        human_head = self.git("rev-parse", "HEAD", cwd=human).strip()
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale info", result.stderr)
        self.assertEqual(self.remote_head(), human_head)
        self.assertEqual(self.log("GH_MOCK_MUTATIONS"), [])
        pushes = [call for call in self.log("GIT_MOCK_LOG") if " push " in " " + call + " "]
        self.assertEqual(len(pushes), 1)
        self.assertIn("--force-with-lease=refs/heads/" + BRANCH + ":" + head, pushes[0])


if __name__ == "__main__":
    unittest.main()
