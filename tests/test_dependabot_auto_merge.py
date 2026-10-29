"""Runs the shell of the reusable Dependabot workflow against a stub `gh`.

The step scripts are read out of .github/workflows/dependabot-auto-merge.yml at
test time, so the test exercises the file that ships, not a copy of it. The stub
answers the API calls the step makes and records every call, and the real `jq`
evaluates the workflow's own `--jq` filters against the canned responses.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml"
ARM_STEP = "Enable auto-merge for a patch or minor update"
SWEEP_STEP = "Close older PRs for the same ecosystem, directory and group"
SWEEP_FIXTURE = ROOT / "tests" / "fixtures" / "sweep-prs.json"

STUB = r"""#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
args=("$@"); jqexpr=""
for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[$i]}" = "--jq" ] && jqexpr="${args[$((i + 1))]}"
done
case "$1 $2" in
  "api repos/org/repo") body="$STUB_REPO_JSON" ;;
  "api repos/org/repo/rules/branches/"*)
    if [ "${STUB_RULES_FAIL:-0}" = 1 ]; then
      echo "gh: Not Found (HTTP 404)" >&2
      exit 1
    fi
    body="$STUB_RULES_JSON" ;;
  "pr merge") exit 0 ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
if [ -n "$jqexpr" ]; then printf '%s' "$body" | jq -r "$jqexpr"; else printf '%s' "$body"; fi
"""

REQUIRED = [{"type": "required_status_checks",
             "parameters": {"required_status_checks": [{"context": "CI"}, {"context": "Security Scan"}]}}]


SWEEP_STUB = r"""#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
if [ "$1 $2" = "pr list" ]; then
  args=("$@"); jqexpr=""
  for ((i = 0; i < ${#args[@]}; i++)); do
    [ "${args[$i]}" = "--jq" ] && jqexpr="${args[$((i + 1))]}"
  done
  jq -r "$jqexpr" "$STUB_PRS"
  exit $?
fi
[ "$1 $2" = "pr close" ] && exit 0
echo "unexpected gh call: $*" >&2
exit 99
"""


def step_script(name):
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step["run"]
    raise AssertionError(f"step {name!r} not found in {WORKFLOW}")


class ArmStep(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        stub = self.tmp / "gh"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.script = self.tmp / "step.sh"
        self.script.write_text(step_script(ARM_STEP))

    def run_step(self, repo=None, rules=None, rules_fail=False, base="main"):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.tmp}:{env['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_REPO_JSON": json.dumps(repo if repo is not None else {"allow_auto_merge": True}),
            "STUB_RULES_JSON": json.dumps(rules if rules is not None else REQUIRED),
            "STUB_RULES_FAIL": "1" if rules_fail else "0",
            "GITHUB_REPOSITORY": "org/repo",
            "GH_TOKEN": "stub",
            "PR_URL": "https://github.com/org/repo/pull/7",
            "METHOD": "squash",
            "BASE": base,
        })
        proc = subprocess.run(["bash", str(self.script)], env=env, capture_output=True, text=True)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return proc, calls

    def merge_calls(self, calls):
        return [c for c in calls if c.startswith("gh pr merge")]

    def test_arms_auto_merge_when_the_branch_has_required_checks(self):
        proc, calls = self.run_step()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.merge_calls(calls), ["gh pr merge --auto --squash https://github.com/org/repo/pull/7"])

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


class SweepStep(unittest.TestCase):
    """The weekly sweep closes PRs and deletes branches, so its selection is pinned here."""

    def run_sweep(self, prs):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        stub = tmp / "gh"
        stub.write_text(SWEEP_STUB)
        stub.chmod(0o755)
        fixture = tmp / "prs.json"
        fixture.write_text(json.dumps(prs))
        script = tmp / "sweep.sh"
        script.write_text(step_script(SWEEP_STEP))
        log = tmp / "calls.log"
        env = dict(os.environ)
        env.update({"PATH": f"{tmp}:{env['PATH']}", "STUB_LOG": str(log), "STUB_PRS": str(fixture),
                    "GITHUB_REPOSITORY": "org/repo", "GH_TOKEN": "stub"})
        work = tmp / "work"
        work.mkdir()
        proc = subprocess.run(["bash", str(script)], env=env, cwd=work, capture_output=True, text=True)
        calls = log.read_text().splitlines() if log.exists() else []
        return proc, [c for c in calls if c.startswith("gh pr close")]

    def test_closes_only_older_prs_of_the_same_grouped_stream(self):
        prs = json.loads(SWEEP_FIXTURE.read_text())
        proc, closes = self.run_sweep(prs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        closed = {c.split()[3]: c for c in closes}
        # 10 -> 12: same github_actions group. 20 and 17 -> 18: the web
        # prod-deps group, newest first. Ungrouped lodash branches (13, 14),
        # all-digit suffixes (15, 16) and a lone group (19) are left alone.
        self.assertEqual(set(closed), {"10", "17", "20"})
        self.assertIn("Superseded by #12", closed["10"])
        self.assertIn("Superseded by #18", closed["17"])
        self.assertIn("Superseded by #18", closed["20"])
        for call in closes:
            self.assertIn("--delete-branch", call)

    def test_does_nothing_without_a_superseded_pr(self):
        prs = [{"number": 1, "headRefName": "dependabot/pip/all-minor-patch-aaaaaaaaaa", "createdAt": "2026-10-01T00:00:00Z"},
               {"number": 2, "headRefName": "dependabot/npm_and_yarn/all-minor-patch-bbbbbbbbbb", "createdAt": "2026-10-02T00:00:00Z"}]
        proc, closes = self.run_sweep(prs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(closes, [])
        self.assertIn("no superseded Dependabot PRs", proc.stdout)


class StepConditions(unittest.TestCase):
    """The step `if:` expressions run on GitHub, not here; these pin their text."""

    def steps(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        return {s["name"]: s for s in jobs["triage"]["steps"] if "name" in s}

    def test_github_actions_updates_are_never_armed(self):
        cond = " ".join(self.steps()[ARM_STEP]["if"].split())
        self.assertIn("steps.meta.outputs.package-ecosystem != 'github_actions'", cond)
        self.assertIn("inputs.auto-merge", cond)

    def test_majors_are_never_armed(self):
        cond = self.steps()[ARM_STEP]["if"]
        self.assertNotIn("semver-major", cond)


if __name__ == "__main__":
    unittest.main()
