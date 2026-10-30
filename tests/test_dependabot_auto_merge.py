"""Runs the shell of the reusable Dependabot workflow against a stub `gh`.

The step scripts are read out of .github/workflows/dependabot-auto-merge.yml at
test time, so the test exercises the file that ships, not a copy of it. The stub
answers the API calls the steps make and records every call, and the real `jq`
evaluates the workflow's own `--jq` filters against the canned responses.
`Triage` runs every step of the triage job in order, and evaluates the step
`env` and `if:` expressions as GitHub does for the few forms that job uses.
"""

import dataclasses
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml"
ARM_STEP = "Enable auto-merge for a patch or minor update"
SWEEP_STEP = "Close older PRs for the same branch key, group, directories and base"
SWEEP_FIXTURE = ROOT / "tests" / "fixtures" / "sweep-prs.json"
PR_URL = "https://github.com/org/repo/pull/7"

MAJOR = "version-update:semver-major"
MINOR = "version-update:semver-minor"
PATCH = "version-update:semver-patch"

STUB = r"""#!/usr/bin/env bash
"$STUB_PYTHON" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$STUB_LOG"
args=("$@"); jqexpr=""
for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[$i]}" = "--jq" ] && jqexpr="${args[$((i + 1))]}"
done
# A label exists when the repository listing has it, or when another run
# created it after that listing (STUB_LATE_LABELS). Like GitHub, the stub
# matches label names without regard to case.
exists() { grep -qixF -- "$1" "$STUB_LABELS_FILE" || grep -qixF -- "$1" <<<"${STUB_LATE_LABELS:-}"; }
if [ "$1 $2" = "pr merge" ] && [ "${STUB_ARM_FAIL:-0}" = 1 ]; then
  for arg in "$@"; do
    if [ "$arg" = --auto ]; then
      echo "GraphQL: Head sha didn't match expected head sha (enablePullRequestAutoMerge)" >&2
      exit 1
    fi
  done
fi
case "$1 $2" in
  "api repos/org/repo") body="$STUB_REPO_JSON" ;;
  "api repos/org/repo/rules/branches/"*)
    if [ "${STUB_RULES_FAIL:-0}" = 1 ]; then
      echo "gh: Not Found (HTTP 404)" >&2
      exit 1
    fi
    body="$STUB_RULES_JSON" ;;
  "api repos/org/repo/labels?per_page=100") body=$(jq -Rn '[inputs | {name: .}]' "$STUB_LABELS_FILE") ;;
  "api repos/org/repo/pulls/7/files"*)
    if [ "${STUB_FILES_FAIL:-0}" = 1 ]; then
      echo "gh: Server Error (HTTP 500)" >&2
      exit 1
    fi
    # Like the API: 3000 files at most, and per_page files a page (30 by
    # default). Without --paginate, gh returns page 1.
    limit=$(sed -n 's/.*[?&]per_page=\([0-9]*\).*/\1/p' <<<"$2")
    limit=${limit:-30}
    for arg in "$@"; do [ "$arg" = --paginate ] && limit=3000; done
    body=$(jq -c --argjson n "$limit" '.[:$n]' "$STUB_FILES_FILE") ;;
  "api repos/org/repo/pulls/7/commits"*)
    if [ "${STUB_COMMITS_FAIL:-0}" = 1 ]; then
      echo "gh: Server Error (HTTP 500)" >&2
      exit 1
    fi
    # Like the API: oldest commit first, 250 commits at most, and per_page
    # commits a page (30 by default). Without --paginate, gh returns page 1.
    limit=$(sed -n 's/.*[?&]per_page=\([0-9]*\).*/\1/p' <<<"$2")
    limit=${limit:-30}
    for arg in "$@"; do [ "$arg" = --paginate ] && limit=250; done
    body=$(jq -c --argjson n "$limit" '.[:$n]' "$STUB_COMMITS_FILE") ;;
  "api repos/org/repo/labels/"*)
    name=$("$STUB_PYTHON" -c 'import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' \
      "${2#repos/org/repo/labels/}")
    exists "$name" && exit 0
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1 ;;
  "label create")
    if [ "${STUB_CREATE_FAIL:-0}" = 1 ]; then
      echo "HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    # Like gh, refuse to replace a label that the repository has, unless --force.
    if exists "$3"; then
      for arg in "$@"; do [ "$arg" = --force ] && exit 0; done
      echo "label with name \"$3\" already exists; use \`--force\` to update its color and description" >&2
      exit 1
    fi
    printf '%s\n' "$3" >> "$STUB_LABELS_FILE"
    exit 0 ;;
  "pr view") body="$STUB_PR_JSON" ;;
  "pr edit")
    # Like GitHub, refuse a label that the repository does not have.
    for ((i = 0; i < ${#args[@]}; i++)); do
      if [ "${args[$i]}" = --add-label ] && ! exists "${args[$((i + 1))]}"; then
        echo "could not add label: '${args[$((i + 1))]}' not found" >&2
        exit 1
      fi
    done
    exit 0 ;;
  "pr merge" | "pr comment") exit 0 ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
if [ -n "$jqexpr" ]; then printf '%s' "$body" | jq -r "$jqexpr"; else printf '%s' "$body"; fi
"""

# PR commits as the API lists them. Dependabot's commits are verified.
DEPENDABOT_COMMIT = {"sha": "d" * 40, "author": {"login": "dependabot[bot]"},
                     "committer": {"login": "web-flow"}, "commit": {"verification": {"verified": True}}}
PERSON_COMMIT = {"sha": "e" * 40, "author": {"login": "octocat"},
                 "committer": {"login": "octocat"}, "commit": {"verification": {"verified": True}}}

REQUIRED = [{"type": "required_status_checks",
             "parameters": {"required_status_checks": [{"context": "CI"}, {"context": "Security Scan"}]}}]


SWEEP_STUB = r"""#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
args=("$@"); jqexpr="" fields=""
for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[$i]}" = "--jq" ] && jqexpr="${args[$((i + 1))]}"
  [ "${args[$i]}" = "--json" ] && fields="${args[$((i + 1))]}"
done
case "$1 $2" in
  "pr list")
    # Like gh, give each PR only the fields that --json names.
    body=$(jq -c --arg fields "$fields" 'map(with_entries(select(.key | IN($fields | split(",")[]))))' "$STUB_PRS") ;;
  "api repos/org/repo/pulls/"*)
    # The files or the commits of one fixture PR (STUB_FAIL_READS lists
    # <number>/<files or commits> reads that fail). Like the API: per_page
    # entries a page (30 by default), page 1 only without --paginate, at most
    # 3000 files or 250 commits, and the oldest commit first.
    read -r number endpoint < <(sed -E 's|^repos/org/repo/pulls/([0-9]+)/([a-z]+)(\?.*)?$|\1 \2|' <<<"$2")
    case "$endpoint" in
      files) max=3000 ;;
      commits) max=250 ;;
      *) echo "unexpected gh call: $*" >&2; exit 99 ;;
    esac
    if grep -qxF -- "$number/$endpoint" <<<"${STUB_FAIL_READS:-}"; then
      echo "gh: Server Error (HTTP 500)" >&2
      exit 1
    fi
    limit=$(sed -n 's/.*[?&]per_page=\([0-9]*\).*/\1/p' <<<"$2")
    limit=${limit:-30}
    for arg in "$@"; do [ "$arg" = --paginate ] && limit=$max; done
    # A fixture PR lists its files by name and its commits as the API does.
    body=$(jq -c --argjson number "$number" --arg endpoint "$endpoint" --argjson limit "$limit" \
      '(first(.[] | select(.number == $number)) // error("no fixture PR #\($number)"))
       | if $endpoint == "files" then [.files[] | {filename: .}] else .commits end | .[:$limit]' "$STUB_PRS") \
      || exit 99 ;;
  "pr close") exit 0 ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
if [ -n "$jqexpr" ]; then printf '%s' "$body" | jq -r "$jqexpr"; else printf '%s' "$body"; fi
"""

# Two PRs of one group in one directory, on one base branch. The newer PR
# changes every file that the older PR changes, so the older PR is superseded
# when it has only Dependabot's commits.
OLDER = {"number": 61, "headRefName": "dependabot/npm_and_yarn/web/prod-deps-0123abcdef",
         "title": "Bump the prod-deps group in /web with 2 updates", "createdAt": "2026-10-19T00:00:00Z",
         "baseRefName": "main", "files": ["web/package.json", "web/package-lock.json"]}
NEWER = {"number": 62, "headRefName": "dependabot/npm_and_yarn/web/prod-deps-4567abcdef",
         "title": "Bump the prod-deps group in /web with 3 updates", "createdAt": "2026-10-26T00:00:00Z",
         "baseRefName": "main", "files": ["web/package.json", "web/package-lock.json"]}


def step_script(name):
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step["run"]
    raise AssertionError(f"step {name!r} not found in {WORKFLOW}")


def install_stub(tmp, repo_labels=()):
    """Puts the stub `gh` first on PATH and returns the environment for a step."""
    stub = tmp / "gh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    labels = tmp / "repo-labels.txt"
    labels.write_text("".join(f"{name}\n" for name in repo_labels))
    commits = tmp / "commits.json"
    commits.write_text(json.dumps([DEPENDABOT_COMMIT]))
    files = tmp / "files.json"
    files.write_text(json.dumps([{"filename": "package.json"}]))
    env = dict(os.environ)
    env.update({
        "PATH": f"{tmp}:{env['PATH']}",
        "STUB_LOG": str(tmp / "calls.log"),
        "STUB_PYTHON": sys.executable,
        "STUB_LABELS_FILE": str(labels),
        "STUB_LATE_LABELS": "",
        "STUB_CREATE_FAIL": "0",
        "STUB_REPO_JSON": json.dumps({"allow_auto_merge": True}),
        "STUB_RULES_JSON": json.dumps(REQUIRED),
        "STUB_RULES_FAIL": "0",
        "STUB_PR_JSON": json.dumps({"labels": [], "autoMergeRequest": None}),
        "STUB_FILES_FILE": str(files),
        "STUB_FILES_FAIL": "0",
        "STUB_COMMITS_FILE": str(commits),
        "STUB_COMMITS_FAIL": "0",
        "GITHUB_REPOSITORY": "org/repo",
        "GH_TOKEN": "stub",
    })
    return env


def read_calls(log):
    """The gh calls that the stub recorded, each as its argument list."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


class ArmStep(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = install_stub(self.tmp)
        self.script = self.tmp / "step.sh"
        self.script.write_text(step_script(ARM_STEP))

    def run_step(self, repo=None, rules=None, rules_fail=False, base="main", head="d" * 40):
        env = dict(self.env)
        env.update({
            "STUB_REPO_JSON": json.dumps(repo if repo is not None else {"allow_auto_merge": True}),
            "STUB_RULES_JSON": json.dumps(rules if rules is not None else REQUIRED),
            "STUB_RULES_FAIL": "1" if rules_fail else "0",
            "PR_URL": PR_URL,
            "METHOD": "squash",
            "BASE": base,
            "HEAD_SHA": head,
        })
        proc = subprocess.run(["bash", str(self.script)], env=env, capture_output=True, text=True)
        calls = [" ".join(["gh", *call]) for call in read_calls(self.tmp / "calls.log")]
        return proc, calls

    def merge_calls(self, calls):
        return [c for c in calls if c.startswith("gh pr merge")]

    def test_arms_auto_merge_when_the_branch_has_required_checks(self):
        proc, calls = self.run_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.merge_calls(calls), ["gh pr merge --auto --squash --match-head-commit " + "d" * 40 +
                                                   " https://github.com/org/repo/pull/7"])

    def test_refuses_to_arm_without_a_checked_head_commit(self):
        # gh drops an empty --match-head-commit, which would arm any head.
        proc, calls = self.run_step(head="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.merge_calls(calls), [])

    def test_refuses_a_branch_with_no_rules(self):
        proc, calls = self.run_step(rules=[])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no required status checks", proc.stdout)
        self.assertEqual(self.merge_calls(calls), [])

    def test_refuses_a_ruleset_without_required_checks(self):
        proc, calls = self.run_step(rules=[{"type": "pull_request", "parameters": {}}, {"type": "deletion"}])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.merge_calls(calls), [])

    def test_fails_loud_when_the_rules_read_fails(self):
        proc, calls = self.run_step(rules_fail=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.merge_calls(calls), [])

    def test_refuses_a_repo_that_disallows_auto_merge(self):
        proc, calls = self.run_step(repo={"allow_auto_merge": False})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not allow auto-merge", proc.stdout)
        self.assertEqual(self.merge_calls(calls), [])
        self.assertFalse(any("rules/branches" in c for c in calls))

    def test_encodes_a_branch_name_with_a_slash(self):
        proc, calls = self.run_step(base="release/1.0 rc")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rules = [c for c in calls if "rules/branches" in c]
        self.assertEqual(len(rules), 1)
        self.assertIn("rules/branches/release%2F1.0%20rc", rules[0])


# fetch-metadata's outputs, from its action.yml at the commit that the workflow
# pins (v3.1.0). GitHub renders a reference to any other output as an empty
# string, so the job runner below refuses one.
FETCH_METADATA_OUTPUTS = frozenset({
    "dependency-names", "dependency-type", "update-type", "updated-dependencies-json",
    "directory", "package-ecosystem", "target-branch", "previous-version", "new-version",
    "alert-state", "ghsa-id", "cvss", "compatibility-score", "maintainer-changes",
    "dependency-group",
})

# The event context that the triage job reads.
CONTEXT = {
    "github.event.pull_request.html_url": PR_URL,
    "github.event.pull_request.number": "7",
    "github.event.pull_request.base.ref": "main",
    "secrets.GITHUB_TOKEN": "stub",
}

EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
STEP_OUTPUT = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+)")
# One token of an `if:` expression, a space, or (group 2) anything the runner cannot read.
TOKEN = re.compile(r"\s+|(\|\||&&|==|!=|!|\(|\)|'[^']*'|[A-Za-z_][\w.-]*(?:\(\))?)|(.)")
# Words that tell a reader that a PR merges without a person.
AUTO_MERGE_WORDS = re.compile(r"auto(matic(ally)?)?[- ]?merg", re.IGNORECASE)
HOLD_GROUPS = " web-tools , calendar-versioned "
ARMED = [["pr", "merge", "--auto", "--squash", "--match-head-commit", DEPENDABOT_COMMIT["sha"], PR_URL]]
DISARMED = [["pr", "merge", "--disable-auto", PR_URL]]
# gh reports the bot that a workflow's GITHUB_TOKEN acts as like this.
WORKFLOW_BOT = "app/github-actions"


@dataclasses.dataclass
class JobRun:
    calls: list
    stdout: str
    failed: str | None

    def merges(self):
        """Every `gh pr merge` call, whether it arms auto-merge or turns it off."""
        return [call for call in self.calls if call[:2] == ["pr", "merge"]]

    def arms(self):
        return [call for call in self.merges() if "--auto" in call]

    def disarms(self):
        return [call for call in self.merges() if "--disable-auto" in call]

    def added_labels(self):
        return [call[call.index("--add-label") + 1] for call in self.calls
                if call[:2] == ["pr", "edit"] and "--add-label" in call]

    def removed_labels(self):
        return [call[call.index("--remove-label") + 1] for call in self.calls
                if call[:2] == ["pr", "edit"] and "--remove-label" in call]

    def created_labels(self):
        return [call[2] for call in self.calls if call[:2] == ["label", "create"]]

    def comments(self):
        return [call[call.index("--body") + 1] for call in self.calls if call[:2] == ["pr", "comment"]]

    def warnings(self):
        return [line for line in self.stdout.splitlines() if line.startswith("::warning::")]


class Triage(unittest.TestCase):
    """Runs the triage job for one Dependabot PR, step by step, as GitHub does.

    Each test stands in for fetch-metadata with the outputs it gives. The runner
    knows only the expression forms that the job uses, and any other form fails
    the test instead of being guessed at.
    """

    def run_job(self, update_type, ecosystem="npm_and_yarn", group="", inputs=None, updates=None,
                pr_labels=(), repo_labels=(), late_labels=(), create_fail=False, auto_merge_by=None,
                files=("package.json",), files_fail=False, meta_fails=False, commits=None,
                commits_fail=False, event_head=None, arm_fails=False, rules=None):
        doc = yaml.safe_load(WORKFLOW.read_text())
        declared = doc[True]["workflow_call"]["inputs"]  # PyYAML reads the key `on` as True.
        values = {name: spec.get("default", "") for name, spec in declared.items()}
        for name, value in (inputs or {}).items():
            self.assertIn(name, declared, f"the workflow has no input {name!r}")
            values[name] = value
        # One dependency of the reported type, unless the test lists each one.
        dependencies = [{"dependencyName": f"dep-{index}", "dependencyType": "direct:production",
                         "updateType": dependency_type}
                        for index, dependency_type in enumerate(updates or [update_type])]
        outcomes = {}
        outputs = {"meta": {"update-type": update_type, "package-ecosystem": ecosystem,
                            "dependency-group": group, "dependency-names": "left-pad",
                            "updated-dependencies-json": json.dumps(dependencies)}}

        def evaluate(expression):
            if expression.startswith("inputs."):
                name = expression.removeprefix("inputs.")
                self.assertIn(name, values, f"the workflow has no input {name!r}")
                value = values[name]
                return str(value).lower() if isinstance(value, bool) else str(value)
            step_outcome = re.fullmatch(r"steps\.([\w-]+)\.outcome", expression)
            if step_outcome:
                return outcomes.get(step_outcome.group(1), "")
            step_output = STEP_OUTPUT.fullmatch(expression)
            if step_output:
                step, name = step_output.groups()
                if step == "meta":
                    self.assertIn(name, FETCH_METADATA_OUTPUTS, f"fetch-metadata has no output {name!r}")
                return outputs.get(step, {}).get(name, "")
            self.assertIn(expression, context, f"the job runner cannot evaluate {expression!r}")
            return context[expression]

        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        base_env = install_stub(tmp, repo_labels)
        # The shape that `gh pr view --json autoMergeRequest` prints.
        auto_merge = None if auto_merge_by is None else {
            "mergeMethod": "SQUASH",
            "enabledBy": {"is_bot": auto_merge_by.startswith("app/"), "login": auto_merge_by}}
        base_env.update({
            "STUB_PR_JSON": json.dumps({"labels": [{"name": name} for name in pr_labels],
                                        "autoMergeRequest": auto_merge}),
            "STUB_LATE_LABELS": "\n".join(late_labels),
            "STUB_CREATE_FAIL": "1" if create_fail else "0",
            "STUB_FILES_FAIL": "1" if files_fail else "0",
            "STUB_COMMITS_FAIL": "1" if commits_fail else "0",
            "STUB_ARM_FAIL": "1" if arm_fails else "0",
        })
        if rules is not None:
            base_env["STUB_RULES_JSON"] = json.dumps(rules)
        pathlib.Path(base_env["STUB_FILES_FILE"]).write_text(json.dumps([{"filename": name} for name in files]))
        # The head commit that the event names: the PR's newest commit, unless
        # the test moves the head after the event.
        context = dict(CONTEXT)
        context["github.event.pull_request.head.sha"] = (
            event_head if event_head is not None else (commits or [DEPENDABOT_COMMIT])[-1]["sha"])
        if commits is not None:
            pathlib.Path(base_env["STUB_COMMITS_FILE"]).write_text(json.dumps(commits))

        def should_run(step, job_failed):
            """Evaluates `if:` as GitHub does, for a subset of its expression syntax.

            Without a status function, success() applies. Nothing is cancelled here.
            """
            text = " ".join(str(step.get("if", "")).split())
            wrapped = re.fullmatch(r"\$\{\{ (.*) \}\}", text)
            text = wrapped.group(1) if wrapped else text
            tokens = []
            for match in TOKEN.finditer(text):
                self.assertIsNone(match.group(2), f"the job runner cannot read if: {text!r}")
                if match.group(1):
                    tokens.append(match.group(1))
            status = {"success()": not job_failed, "failure()": job_failed, "cancelled()": False, "always()": True}

            def disjunction(i):
                value, i = conjunction(i)
                while i < len(tokens) and tokens[i] == "||":
                    right, i = conjunction(i + 1)
                    value = value or right
                return value, i

            def conjunction(i):
                value, i = negation(i)
                while i < len(tokens) and tokens[i] == "&&":
                    right, i = negation(i + 1)
                    value = value and right
                return value, i

            def negation(i):
                if tokens[i] == "!":
                    value, i = negation(i + 1)
                    return not value, i
                return primary(i)

            def primary(i):
                if tokens[i] == "(":
                    value, i = disjunction(i + 1)
                    self.assertEqual(tokens[i], ")", text)
                    return value, i + 1
                if tokens[i] in status:
                    return status[tokens[i]], i + 1
                operand, operator, literal = tokens[i:i + 3]
                self.assertIn(operator, ("==", "!="), text)
                self.assertRegex(literal, r"^'[^']*'$", text)
                # GitHub compares strings without regard to case.
                equal = evaluate(operand).lower() == literal[1:-1].lower()
                return equal == (operator == "=="), i + 3

            value, end = disjunction(0) if tokens else (True, 0)
            self.assertEqual(end, len(tokens), text)
            if not any(token in status for token in tokens):
                value = value and not job_failed
            return value

        stdout, failed = [], None
        for step in doc["jobs"]["triage"]["steps"]:
            if "uses" in step:
                self.assertEqual(step.get("id"), "meta", "only fetch-metadata runs as an action")
                outcomes["meta"] = "success"
                if meta_fails:
                    # As when the oldest commit is not a verified Dependabot commit.
                    outputs.pop("meta")
                    outcomes["meta"] = "failure"
                    failed = failed or f"{step['name']}: PR is not from Dependabot, nothing to do."
                continue
            self.assertLessEqual(set(step), {"name", "id", "if", "env", "run"}, step.get("name"))
            if not should_run(step, failed is not None):
                if "id" in step:
                    outcomes[step["id"]] = "skipped"
                continue
            self.assertNotIn("${{", step["run"], f"{step['name']}: read expressions through env")
            env = dict(base_env)
            for key, template in step.get("env", {}).items():
                env[key] = EXPRESSION.sub(lambda match: evaluate(match.group(1)), str(template))
            output = tmp / "output"
            output.write_text("")
            env["GITHUB_OUTPUT"] = str(output)
            script = tmp / "step.sh"
            script.write_text(step["run"])
            # GitHub runs a `run:` step with `bash -e` when the step names no shell.
            proc = subprocess.run(["bash", "-e", str(script)], env=env, cwd=tmp,
                                  capture_output=True, text=True)
            stdout.append(proc.stdout)
            if "id" in step:
                outcomes[step["id"]] = "success" if proc.returncode == 0 else "failure"
            if proc.returncode != 0:
                failed = failed or f"{step['name']}: {proc.stderr}"
                continue
            if "id" in step:
                outputs[step["id"]] = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return JobRun(read_calls(tmp / "calls.log"), "".join(stdout), failed)

    def test_arms_a_patch_or_minor_update(self):
        # arm-github-actions is off by default and does not affect other ecosystems.
        for update_type in (PATCH, MINOR):
            with self.subTest(update_type=update_type):
                run = self.run_job(update_type)
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), ARMED)
                self.assertEqual(run.added_labels(), [])

    def test_arms_a_pr_whose_dependencies_all_have_known_types(self):
        run = self.run_job(MINOR, updates=[PATCH, MINOR, PATCH])
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), ARMED)
        self.assertEqual(run.added_labels(), [])

    def test_treats_a_pr_with_an_unreadable_dependency_as_unknown(self):
        # fetch-metadata reports the highest type it could read, so a patch
        # entry hides an entry whose type is empty.
        for reported, updates in ((PATCH, [PATCH, ""]), (MINOR, ["", MINOR])):
            with self.subTest(updates=updates):
                run = self.run_job(reported, updates=updates)
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(run.added_labels(), ["dependencies: review"])
                self.assertEqual(len(run.comments()), 1)
                self.assertEqual(len(run.warnings()), 1, run.stdout)

    def test_arms_a_github_actions_update_when_arm_github_actions_is_on(self):
        for update_type in (PATCH, MINOR):
            with self.subTest(update_type=update_type):
                run = self.run_job(update_type, ecosystem="github_actions", inputs={"arm-github-actions": True})
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), ARMED)
                self.assertEqual(run.added_labels(), ["dependencies: actions"])

    def test_holds_a_github_actions_update_unless_both_inputs_are_on(self):
        for inputs in ({}, {"arm-github-actions": False}, {"auto-merge": False, "arm-github-actions": True}):
            for update_type in (PATCH, MINOR):
                with self.subTest(inputs=inputs, update_type=update_type):
                    run = self.run_job(update_type, ecosystem="github_actions", inputs=inputs)
                    self.assertIsNone(run.failed)
                    self.assertEqual(run.arms(), [])
                    self.assertEqual(sorted(run.added_labels()), ["dependencies: actions", "dependencies: ready"])

    def test_holds_a_pr_with_a_commit_that_dependabot_did_not_make(self):
        # fetch-metadata checks only the oldest commit, and the API lists the
        # oldest commit first, so a person's fix pushed on top passes there.
        for update_type, labels in ((PATCH, ["dependencies: ready"]),
                                    (MAJOR, ["dependencies: major", "dependencies: ready"])):
            with self.subTest(update_type=update_type):
                run = self.run_job(update_type, commits=[DEPENDABOT_COMMIT, PERSON_COMMIT],
                                   auto_merge_by=WORKFLOW_BOT)
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(run.disarms(), DISARMED)
                self.assertEqual(sorted(run.added_labels()), labels)

    def test_holds_a_pr_with_an_unverified_or_unlinked_commit(self):
        unverified = dict(DEPENDABOT_COMMIT, sha="f" * 40, commit={"verification": {"verified": False}})
        # The API gives no author when the commit email belongs to no account.
        unlinked = dict(DEPENDABOT_COMMIT, sha="c" * 40, author=None)
        for commit in (unverified, unlinked):
            with self.subTest(commit=commit["sha"][:1]):
                run = self.run_job(PATCH, commits=[DEPENDABOT_COMMIT, commit])
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(run.added_labels(), ["dependencies: ready"])

    def test_holds_a_dependabot_commit_that_a_maintainer_amended(self):
        # GitHub verifies the committer's signature. An amend or rebase with
        # signing on keeps the author dependabot[bot] but not the committer.
        amended = dict(DEPENDABOT_COMMIT, sha="a" * 40, committer={"login": "octocat"})
        for commits in ([amended], [DEPENDABOT_COMMIT, amended]):
            with self.subTest(commits=len(commits)):
                run = self.run_job(PATCH, commits=commits, auto_merge_by=WORKFLOW_BOT)
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(run.disarms(), DISARMED)
                self.assertEqual(run.added_labels(), ["dependencies: ready"])

    def test_arms_only_the_newest_commit_that_it_checked(self):
        # The API lists the oldest commit first.
        oldest, newest = dict(DEPENDABOT_COMMIT, sha="1" * 40), dict(DEPENDABOT_COMMIT, sha="2" * 40)
        run = self.run_job(PATCH, commits=[oldest, newest])
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), [["pr", "merge", "--auto", "--squash", "--match-head-commit", "2" * 40, PR_URL]])

    def test_flags_a_pr_that_fetch_metadata_rejects_for_review(self):
        # fetch-metadata fails when the oldest commit is not a verified
        # Dependabot commit, so the decide step does not run.
        with self.subTest("first run"):
            run = self.run_job(PATCH, meta_fails=True, commits=[PERSON_COMMIT, DEPENDABOT_COMMIT],
                               auto_merge_by=WORKFLOW_BOT)
            self.assertEqual(run.added_labels(), ["dependencies: review"])
            [comment] = run.comments()
            self.assertRegex(comment, r"(?i)\breview")
            self.assertEqual(run.disarms(), DISARMED)
            self.assertEqual(run.arms(), [])
        with self.subTest("the PR has the review label already"):
            run = self.run_job(PATCH, meta_fails=True, pr_labels=["Dependencies: Review"],
                               repo_labels=["Dependencies: Review"])
            self.assertEqual(run.added_labels(), [])
            self.assertEqual(run.comments(), [])
        with self.subTest("the PR has the hold label from an earlier run"):
            run = self.run_job(PATCH, meta_fails=True, pr_labels=["dependencies: ready"],
                               repo_labels=["dependencies: ready"])
            self.assertEqual(run.removed_labels(), ["dependencies: ready"])
            self.assertEqual(run.added_labels(), ["dependencies: review"])

    def test_arms_a_pr_whose_commits_are_all_verified_dependabot_commits(self):
        run = self.run_job(PATCH, commits=[DEPENDABOT_COMMIT] * 3)
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), ARMED)
        self.assertEqual(run.added_labels(), [])

    def test_reads_every_page_of_commits(self):
        # 150 commits take more than one page of 30, or of 100.
        with self.subTest("a person's commit on the last page"):
            run = self.run_job(PATCH, commits=[DEPENDABOT_COMMIT] * 149 + [PERSON_COMMIT])
            self.assertIsNone(run.failed)
            self.assertEqual(run.arms(), [])
            self.assertEqual(run.added_labels(), ["dependencies: ready"])
        with self.subTest("every commit is Dependabot's"):
            run = self.run_job(PATCH, commits=[DEPENDABOT_COMMIT] * 150)
            self.assertIsNone(run.failed)
            self.assertEqual(run.arms(), ARMED)

    def test_holds_a_pr_with_more_commits_than_the_api_lists(self):
        # The API lists at most 250 commits, so newer ones would go unchecked.
        run = self.run_job(PATCH, commits=[DEPENDABOT_COMMIT] * 260)
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), [])
        self.assertEqual(run.added_labels(), ["dependencies: ready"])

    def test_fails_closed_when_the_commits_cannot_be_read(self):
        run = self.run_job(PATCH, commits_fail=True, auto_merge_by=WORKFLOW_BOT)
        self.assertIsNotNone(run.failed)
        self.assertEqual(run.arms(), [])
        self.assertEqual(run.disarms(), DISARMED)

    def test_treats_a_renamed_actions_ecosystem_as_github_actions(self):
        # fetch-metadata reads the ecosystem from the branch name. With the
        # pull-request-branch-name options, separator "_" leaves "github",
        # word-separator "-" gives "github-actions", and branch-name-case
        # changes the letter case.
        for ecosystem in ("github", "github-actions", "GITHUB_ACTIONS", "GitHub-Actions"):
            with self.subTest(ecosystem=ecosystem):
                run = self.run_job(MINOR, ecosystem=ecosystem)
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(sorted(run.added_labels()), ["dependencies: actions", "dependencies: ready"])

    def test_treats_a_pr_that_changes_a_workflow_or_action_file_as_github_actions(self):
        # A branch-name template can drop the ecosystem from the branch name.
        for files in ([".github/workflows/ci.yml"], ["tools/setup/action.yml"], ["action.yaml"],
                      ["package.json", ".github/workflows/release.yaml"]):
            with self.subTest(files=files):
                run = self.run_job(MINOR, ecosystem="deps", files=files)
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(sorted(run.added_labels()), ["dependencies: actions", "dependencies: ready"])

    def test_treats_a_pr_with_more_files_than_the_api_lists_as_github_actions(self):
        # The API lists at most 3000 files, so it does not list this workflow file.
        files = [f"src/module-{index:04d}.js" for index in range(3000)] + [".github/workflows/ci.yml"]
        run = self.run_job(MINOR, ecosystem="npm_and_yarn", files=files)
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), [])
        self.assertEqual(sorted(run.added_labels()), ["dependencies: actions", "dependencies: ready"])

    def test_arms_a_pr_whose_files_only_look_like_actions_files(self):
        run = self.run_job(MINOR, ecosystem="deps",
                           files=["docs/action.yml.md", "docs/github/workflows/ci.yml", "myaction.yml"])
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), ARMED)
        self.assertEqual(run.added_labels(), [])

    def test_fails_closed_when_the_changed_files_cannot_be_read(self):
        run = self.run_job(MINOR, ecosystem="deps", files_fail=True, auto_merge_by=WORKFLOW_BOT)
        self.assertIsNotNone(run.failed)
        self.assertEqual(run.arms(), [])
        # The step that turns off auto-merge still runs after the failure.
        self.assertEqual(run.disarms(), DISARMED)

    def test_turns_off_bot_auto_merge_when_metadata_cannot_be_read(self):
        # fetch-metadata fails when the oldest commit of the PR is not a
        # verified Dependabot commit, for example after a person rewrites the
        # branch.
        with self.subTest("the workflow's bot enabled it"):
            run = self.run_job(PATCH, meta_fails=True, auto_merge_by=WORKFLOW_BOT)
            self.assertIsNotNone(run.failed)
            self.assertEqual(run.disarms(), DISARMED)
            self.assertEqual(run.arms(), [])
        with self.subTest("a person enabled it"):
            run = self.run_job(PATCH, meta_fails=True, auto_merge_by="octocat")
            self.assertEqual(run.disarms(), [])

    def test_removes_a_hold_or_review_label_that_no_longer_applies(self):
        with self.subTest("arm-github-actions turned on"):
            labels = ["dependencies: actions", "Dependencies: Ready"]
            run = self.run_job(PATCH, ecosystem="github_actions", inputs={"arm-github-actions": True},
                               pr_labels=labels, repo_labels=labels)
            self.assertIsNone(run.failed)
            self.assertEqual(run.removed_labels(), ["Dependencies: Ready"])
            self.assertEqual(run.arms(), ARMED)
        with self.subTest("the update type became known"):
            run = self.run_job(PATCH, inputs={"auto-merge": False}, pr_labels=["DEPENDENCIES: REVIEW"],
                               repo_labels=["DEPENDENCIES: REVIEW"])
            self.assertIsNone(run.failed)
            self.assertEqual(run.removed_labels(), ["DEPENDENCIES: REVIEW"])
            self.assertEqual(run.added_labels(), ["dependencies: ready"])
        with self.subTest("the labels still apply"):
            run = self.run_job(PATCH, inputs={"auto-merge": False}, pr_labels=["dependencies: ready"],
                               repo_labels=["dependencies: ready"])
            self.assertIsNone(run.failed)
            self.assertEqual(run.removed_labels(), [])
        with self.subTest("hold-label and unknown-label are one label"):
            one = {"hold-label": "needs a person", "unknown-label": "needs a person"}
            run = self.run_job("", inputs=one, pr_labels=["needs a person"], repo_labels=["needs a person"])
            self.assertIsNone(run.failed)
            self.assertEqual(run.removed_labels(), [])

    def test_marks_every_github_actions_update(self):
        for update_type in (PATCH, MINOR, MAJOR, ""):
            for auto_merge in (True, False):
                with self.subTest(update_type=update_type, auto_merge=auto_merge):
                    run = self.run_job(update_type, ecosystem="github_actions",
                                       inputs={"auto-merge": auto_merge, "arm-github-actions": True})
                    self.assertIsNone(run.failed)
                    self.assertIn("dependencies: actions", run.added_labels())

    def test_holds_a_patch_or_minor_update_of_any_ecosystem_when_auto_merge_is_off(self):
        # The repository has no hold label yet, so the job must create it.
        for ecosystem in ("npm_and_yarn", "pip", "cargo", "docker", "github_actions"):
            for update_type in (PATCH, MINOR):
                with self.subTest(ecosystem=ecosystem, update_type=update_type):
                    run = self.run_job(update_type, ecosystem=ecosystem, inputs={"auto-merge": False})
                    self.assertIsNone(run.failed)
                    self.assertEqual(run.arms(), [])
                    self.assertEqual(run.disarms(), [])
                    self.assertIn("dependencies: ready", run.added_labels())

    def test_turns_off_auto_merge_that_an_earlier_run_enabled(self):
        # A new commit can make an armed PR a held, major or unknown one.
        cases = ((PATCH, "npm_and_yarn", "", {"auto-merge": False}),
                 (MINOR, "github_actions", "", {}),
                 (MINOR, "npm_and_yarn", "calendar-versioned", {"hold-groups": HOLD_GROUPS}),
                 (MAJOR, "npm_and_yarn", "", {}),
                 ("", "npm_and_yarn", "", {}))
        for update_type, ecosystem, group, inputs in cases:
            with self.subTest(update_type=update_type, ecosystem=ecosystem, group=group, inputs=inputs):
                run = self.run_job(update_type, ecosystem=ecosystem, group=group, inputs=inputs,
                                   auto_merge_by=WORKFLOW_BOT)
                self.assertIsNone(run.failed)
                self.assertEqual(run.disarms(), DISARMED)
                self.assertEqual(run.arms(), [])

    def test_leaves_auto_merge_that_a_person_enabled(self):
        for update_type in (PATCH, MAJOR, ""):
            with self.subTest(update_type=update_type):
                run = self.run_job(update_type, inputs={"auto-merge": False}, auto_merge_by="octocat")
                self.assertIsNone(run.failed)
                self.assertEqual(run.disarms(), [])

    def test_turns_off_bot_auto_merge_when_arming_fails(self):
        # An earlier run armed the PR. This run decides to arm it, but the arm
        # step fails, so the old arm must not stay on.
        with self.subTest("GitHub refuses the checked head, as after a push"):
            run = self.run_job(PATCH, auto_merge_by=WORKFLOW_BOT, arm_fails=True)
            self.assertIsNotNone(run.failed)
            self.assertEqual(run.arms(), ARMED)
            self.assertEqual(run.disarms(), DISARMED)
        with self.subTest("the ruleset lost its required checks"):
            run = self.run_job(PATCH, auto_merge_by=WORKFLOW_BOT, rules=[])
            self.assertIsNotNone(run.failed)
            self.assertEqual(run.arms(), [])
            self.assertEqual(run.disarms(), DISARMED)
        with self.subTest("a person enabled auto-merge"):
            run = self.run_job(PATCH, auto_merge_by="octocat", rules=[])
            self.assertIsNotNone(run.failed)
            self.assertEqual(run.disarms(), [])

    def test_holds_a_pr_whose_head_moved_after_the_event(self):
        # A force-push between the event and the commit listing: the listed
        # head is not the head that the event names.
        run = self.run_job(PATCH, event_head="9" * 40, auto_merge_by=WORKFLOW_BOT)
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), [])
        self.assertEqual(run.disarms(), DISARMED)
        self.assertEqual(run.added_labels(), ["dependencies: ready"])

    def test_does_not_arm_over_auto_merge_that_a_person_enabled(self):
        # gh would send the mutation again and make the workflow its owner.
        run = self.run_job(PATCH, auto_merge_by="octocat")
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), [])
        self.assertEqual(run.disarms(), [])

    def test_renews_auto_merge_that_an_earlier_run_enabled(self):
        # The arm call again carries the checked head, so GitHub checks it again.
        run = self.run_job(PATCH, auto_merge_by=WORKFLOW_BOT)
        self.assertIsNone(run.failed)
        self.assertEqual(run.disarms(), [])
        self.assertEqual(run.arms(), ARMED)

    def test_keeps_an_existing_label_as_it_is(self):
        run = self.run_job(PATCH, inputs={"auto-merge": False}, repo_labels=["dependencies: ready"])
        self.assertIsNone(run.failed)
        self.assertEqual(run.added_labels(), ["dependencies: ready"])
        self.assertEqual(run.created_labels(), [])
        self.assertFalse(any("--force" in call for call in run.calls))

    def test_creates_only_the_missing_labels(self):
        run = self.run_job(MINOR, ecosystem="github_actions", inputs={"auto-merge": False},
                           repo_labels=["dependencies: actions"])
        self.assertIsNone(run.failed)
        self.assertEqual(sorted(run.added_labels()), ["dependencies: actions", "dependencies: ready"])
        self.assertEqual(run.created_labels(), ["dependencies: ready"])
        self.assertFalse(any("--force" in call for call in run.calls))

    def test_matches_label_names_without_regard_to_case(self):
        with self.subTest("the PR has the label"):
            run = self.run_job(MAJOR, pr_labels=["Dependencies: Major"], repo_labels=["Dependencies: Major"])
            self.assertIsNone(run.failed)
            self.assertEqual(run.created_labels(), [])
            self.assertEqual(run.added_labels(), [])
            self.assertEqual(run.comments(), [])
        with self.subTest("only the repository has the label"):
            run = self.run_job(MAJOR, repo_labels=["Dependencies: Major"])
            self.assertIsNone(run.failed)
            self.assertEqual(run.created_labels(), [])
            self.assertEqual(run.added_labels(), ["dependencies: major"])
            self.assertEqual(len(run.comments()), 1)

    def test_tolerates_a_label_that_another_run_creates(self):
        run = self.run_job(PATCH, inputs={"auto-merge": False}, late_labels=["dependencies: ready"])
        self.assertIsNone(run.failed)
        self.assertEqual(run.added_labels(), ["dependencies: ready"])

    def test_fails_when_a_missing_label_cannot_be_created(self):
        run = self.run_job(PATCH, inputs={"auto-merge": False}, create_fail=True)
        self.assertIsNotNone(run.failed)
        self.assertEqual(run.added_labels(), [])

    def test_applies_the_hold_label_input(self):
        run = self.run_job(PATCH, inputs={"auto-merge": False, "hold-label": "merge by hand"})
        self.assertIsNone(run.failed)
        self.assertEqual(run.added_labels(), ["merge by hand"])

    def test_does_not_label_or_comment_twice(self):
        for update_type, label in ((PATCH, "dependencies: ready"), (MAJOR, "dependencies: major"),
                                   ("", "dependencies: review")):
            with self.subTest(update_type=update_type):
                run = self.run_job(update_type, inputs={"auto-merge": False}, pr_labels=[label],
                                   repo_labels=[label])
                self.assertIsNone(run.failed)
                self.assertEqual(run.added_labels(), [])
                self.assertEqual(run.comments(), [])

    def test_major_comment_mentions_auto_merge_only_when_it_applies(self):
        # For a GitHub Actions PR, patch and minor updates auto-merge only with
        # arm-github-actions on as well.
        cases = (("npm_and_yarn", {"auto-merge": True}, True),
                 ("npm_and_yarn", {"auto-merge": False}, False),
                 ("github_actions", {"auto-merge": True}, False),
                 ("github_actions", {"auto-merge": True, "arm-github-actions": True}, True),
                 ("github_actions", {"auto-merge": False, "arm-github-actions": True}, False))
        for ecosystem, inputs, mentioned in cases:
            with self.subTest(ecosystem=ecosystem, inputs=inputs):
                run = self.run_job(MAJOR, ecosystem=ecosystem, inputs=inputs)
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertIn("dependencies: major", run.added_labels())
                [comment] = run.comments()
                self.assertEqual(bool(AUTO_MERGE_WORDS.search(comment)), mentioned, comment)

    def test_never_arms_a_group_that_hold_groups_lists(self):
        # A date-style version such as 2026.7.1 to 2026.10.5 is a minor update.
        for hold_groups in (HOLD_GROUPS, "web-tools,\ncalendar-versioned\n", "calendar-versioned"):
            with self.subTest(hold_groups=hold_groups):
                run = self.run_job(MINOR, group="calendar-versioned", inputs={"hold-groups": hold_groups})
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(run.added_labels(), ["dependencies: ready"])

    def test_never_arms_a_subgroup_of_a_listed_group(self):
        # group-by: dependency-name names each subgroup <parent>/<dependency>.
        run = self.run_job(MINOR, group="calendar-versioned/some-tool", inputs={"hold-groups": HOLD_GROUPS})
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), [])
        self.assertEqual(run.added_labels(), ["dependencies: ready"])

    def test_arms_a_group_that_hold_groups_does_not_list_in_full(self):
        for group in ("calendar", "versioned", "calendar-versioned-extra", "calendar-versioned-extra/some-tool",
                      "calendar/some-tool", "some-tool/calendar-versioned", ""):
            with self.subTest(group=group):
                run = self.run_job(MINOR, group=group, inputs={"hold-groups": HOLD_GROUPS})
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), ARMED)
                self.assertEqual(run.added_labels(), [])

    def test_holds_a_major_update_in_a_listed_group(self):
        run = self.run_job(MAJOR, group="calendar-versioned", inputs={"hold-groups": HOLD_GROUPS})
        self.assertIsNone(run.failed)
        self.assertEqual(run.arms(), [])
        self.assertEqual(sorted(run.added_labels()), ["dependencies: major", "dependencies: ready"])
        # The group's patch and minor updates do not auto-merge either.
        [comment] = run.comments()
        self.assertIsNone(AUTO_MERGE_WORDS.search(comment), comment)

    def test_flags_an_update_with_no_known_update_type_for_review(self):
        # fetch-metadata leaves update-type empty for a bump that it cannot
        # compare, such as some bumps from one commit SHA to another. Such a PR
        # is not a held patch or minor update, so it never gets the hold label.
        for update_type, group in (("", ""), ("version-update:unknown", ""), ("", "calendar-versioned")):
            with self.subTest(update_type=update_type, group=group):
                run = self.run_job(update_type, group=group, inputs={"hold-groups": HOLD_GROUPS})
                self.assertIsNone(run.failed)
                self.assertEqual(run.arms(), [])
                self.assertEqual(run.added_labels(), ["dependencies: review"])
                [comment] = run.comments()
                self.assertRegex(comment, r"(?i)\btype\b.*\breview")
                self.assertEqual(len(run.warnings()), 1, run.stdout)

    def test_applies_the_unknown_label_input(self):
        run = self.run_job("", inputs={"unknown-label": "needs triage"})
        self.assertIsNone(run.failed)
        self.assertEqual(run.added_labels(), ["needs triage"])


class JobSettings(unittest.TestCase):
    """Settings that only GitHub acts on, so their values are pinned here."""

    def test_runs_for_one_pr_queue_instead_of_overlapping(self):
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["triage"]
        self.assertIn("${{ github.event.pull_request.number }}", job["concurrency"]["group"])
        self.assertIs(job["concurrency"]["cancel-in-progress"], False)


class SweepStep(unittest.TestCase):
    """The weekly sweep closes PRs and deletes branches, so its selection is pinned here."""

    def run_sweep(self, prs, fail_reads=()):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        stub = tmp / "gh"
        stub.write_text(SWEEP_STUB)
        stub.chmod(0o755)
        fixture = tmp / "prs.json"
        # A PR that lists no commits has one verified Dependabot commit.
        fixture.write_text(json.dumps([{"commits": [DEPENDABOT_COMMIT], **pr} for pr in prs]))
        script = tmp / "sweep.sh"
        script.write_text(step_script(SWEEP_STEP))
        log = tmp / "calls.log"
        env = dict(os.environ)
        env.update({"PATH": f"{tmp}:{env['PATH']}", "STUB_LOG": str(log), "STUB_PRS": str(fixture),
                    "STUB_FAIL_READS": "\n".join(fail_reads), "GITHUB_REPOSITORY": "org/repo", "GH_TOKEN": "stub"})
        work = tmp / "work"
        work.mkdir()
        proc = subprocess.run(["bash", str(script)], env=env, cwd=work, capture_output=True, text=True)
        calls = log.read_text().splitlines() if log.exists() else []
        return proc, [c for c in calls if c.startswith("gh pr close")]

    def superseded(self, closes):
        """Each closed PR with the PR that its close comment names, as numbers."""
        return [(c.split()[3], re.search(r"Superseded by #(\d+)", c).group(1)) for c in closes]

    def test_closes_only_older_prs_of_the_same_grouped_stream(self):
        prs = json.loads(SWEEP_FIXTURE.read_text())
        proc, closes = self.run_sweep(prs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        closed = {c.split()[3]: c for c in closes}
        # 10 -> 12: the github_actions group, where 10 has one update and so a
        # "... in the <group> group" title. 15 -> 16: the web dev-deps group,
        # whose hashes are all digits, under a prefixed title. 17 and 20 -> 18:
        # the web prod-deps group, newest first. 25 -> 26: a group on a branch
        # with "-" separators. In each pair, the newer PR changes every file
        # that the older PR changes, 12 and 26 one file more, and every commit
        # is Dependabot's. Ungrouped PRs whose branches end in a version
        # (13, 14), ten digits (21, 22) or ten hex digits (23, 24), a lone
        # group (19), and two pairs that share a "-" branch key are left
        # alone: 27 and 28 (group build-deps in /web, group web-build-deps
        # in /) and 31 and 32 (group deps in /web/build and in /web-build).
        self.assertEqual(set(closed), {"10", "15", "17", "20", "25"})
        self.assertIn("Superseded by #12", closed["10"])
        self.assertIn("Superseded by #16", closed["15"])
        self.assertIn("Superseded by #18", closed["17"])
        self.assertIn("Superseded by #18", closed["20"])
        self.assertIn("Superseded by #26", closed["25"])
        for call in closes:
            self.assertIn("--delete-branch", call)

    def test_does_nothing_without_a_superseded_pr(self):
        prs = [{"number": 1, "headRefName": "dependabot/pip/all-minor-patch-aaaaaaaaaa",
                "title": "Bump the all-minor-patch group with 2 updates", "createdAt": "2026-10-01T00:00:00Z"},
               {"number": 2, "headRefName": "dependabot/npm_and_yarn/all-minor-patch-bbbbbbbbbb",
                "title": "Bump the all-minor-patch group with 3 updates", "createdAt": "2026-10-02T00:00:00Z"}]
        proc, closes = self.run_sweep(prs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(closes, [])
        self.assertIn("no superseded Dependabot PRs", proc.stdout)

    def test_keeps_prs_of_two_ecosystems_whose_branches_share_a_key(self):
        # pull-request-branch-name options can drop the ecosystem from the
        # branch name, so an npm group and a pip group of one name share a key.
        npm = {"number": 41, "headRefName": "dependabot/minor-patch-3f2a9c1b7e",
               "title": "Bump the minor-patch group with 3 updates", "createdAt": "2026-10-19T00:00:00Z",
               "baseRefName": "main", "files": ["package.json", "package-lock.json"]}
        pip = {"number": 42, "headRefName": "dependabot/minor-patch-8d4e6f0a2c",
               "title": "Bump the minor-patch group with 2 updates", "createdAt": "2026-10-19T00:05:00Z",
               "baseRefName": "main", "files": ["requirements.txt"]}
        proc, closes = self.run_sweep([npm, pip])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(closes, [])
        self.assertIn("::notice::#41 stays open", proc.stdout)

    def test_keeps_prs_of_one_group_for_two_base_branches(self):
        # A branch name can lack the target branch, so PRs for two base
        # branches can share a key.
        main = {"number": 51, "headRefName": "dependabot/npm_and_yarn/all-1b2c3d4e5f",
                "title": "Bump the all group with 4 updates", "createdAt": "2026-10-19T00:00:00Z",
                "baseRefName": "main", "files": ["package.json", "package-lock.json"]}
        release = dict(main, number=52, headRefName="dependabot/npm_and_yarn/all-6a7b8c9d0e",
                       title="Bump the all group with 1 update", createdAt="2026-10-19T00:02:00Z",
                       baseRefName="release/2.x")
        with self.subTest("one PR for each base branch"):
            proc, closes = self.run_sweep([main, release])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(closes, [])
        with self.subTest("the newest PR is for the other base branch"):
            newer_main = dict(main, number=53, headRefName="dependabot/npm_and_yarn/all-0f1e2d3c4b",
                              createdAt="2026-10-19T00:01:00Z")
            proc, closes = self.run_sweep([main, release, newer_main])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.superseded(closes), [("51", "53")])

    def test_keeps_an_older_pr_with_a_commit_that_dependabot_did_not_make(self):
        # The sweep deletes the branch of a PR that it closes, so a fix that a
        # person pushed to the older PR would be lost.
        unverified = dict(DEPENDABOT_COMMIT, sha="f" * 40, commit={"verification": {"verified": False}})
        amended = dict(DEPENDABOT_COMMIT, sha="a" * 40, committer={"login": "octocat"})
        for case, commits in (("a person's commit on top", [DEPENDABOT_COMMIT, PERSON_COMMIT]),
                              ("an unverified Dependabot commit", [unverified]),
                              ("a Dependabot commit that a maintainer amended", [DEPENDABOT_COMMIT, amended]),
                              ("more commits than the API lists", [DEPENDABOT_COMMIT] * 260)):
            with self.subTest(case):
                proc, closes = self.run_sweep([dict(OLDER, commits=commits), NEWER])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(closes, [])
                self.assertIn("::notice::#61 stays open", proc.stdout)
        with self.subTest("every commit is a verified Dependabot commit"):
            proc, closes = self.run_sweep([dict(OLDER, commits=[DEPENDABOT_COMMIT] * 3), NEWER])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.superseded(closes), [("61", "62")])

    def test_keeps_an_older_pr_unless_the_newer_one_changes_all_its_files(self):
        vendored = [f"vendor/module-{index:04d}/go.mod" for index in range(3001)]
        cases = (("a file that only the older PR changes", OLDER["files"] + ["web/yarn.lock"], NEWER["files"]),
                 # Without --paginate, gh gives page 1 only, which lacks that file.
                 ("that file on the last page", vendored[:150] + ["web/yarn.lock"], vendored[:150]),
                 # The API lists 3000 files, so the older PR's last file is not listed.
                 ("the older PR has more files than the API lists", vendored, vendored[:3000]),
                 ("the newer PR has more files than the API lists", OLDER["files"], OLDER["files"] + vendored))
        for case, older_files, newer_files in cases:
            with self.subTest(case):
                proc, closes = self.run_sweep([dict(OLDER, files=older_files), dict(NEWER, files=newer_files)])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(closes, [])
                self.assertIn("::notice::#61 stays open", proc.stdout)
        with self.subTest("the newer PR changes one file more"):
            proc, closes = self.run_sweep([OLDER, dict(NEWER, files=NEWER["files"] + ["web/.npmrc"])])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.superseded(closes), [("61", "62")])

    def test_fails_closed_when_files_or_commits_cannot_be_read(self):
        for read in ("61/files", "62/files", "61/commits"):
            with self.subTest(read=read):
                proc, closes = self.run_sweep([OLDER, NEWER], fail_reads=[read])
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(closes, [])


if __name__ == "__main__":
    unittest.main()
